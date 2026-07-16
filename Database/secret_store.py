"""Encryption-at-rest for secrets stored in the shared database.

Spotify session cookies, API client secrets and refresh tokens live in the
users table of a SQLite file the README tells self-hosters to back up and
copy around - stored as plaintext, one leaked backup handed out every user's
live Spotify session. Values are Fernet-encrypted with a key that never
lives inside the database file itself.

Key material resolution order:
1. DATA_ENCRYPTION_KEY env var (dedicated key, rotatable independently)
2. FLASK_SECRET_KEY env var (most Docker deployments already set it - see
   the README's compose example - so encryption needs zero new config there)
3. secrets/data_encryption_key.txt, auto-created with a random value (the
   local-dev fallback; secrets/ already persists the flask key the same way)

The raw material is normalized to a Fernet key via SHA-256, so any non-empty
string works as a key. Encrypted values are stored as "enc:v1:<token>";
values without that prefix are passed through as legacy plaintext (rows
written before encryption existed), and a prefixed value that no longer
decrypts (rotated/lost key) reads as missing - the affected user is routed
through re-login instead of the app crashing on it.
"""
import base64
import hashlib
import logging
import os
import secrets
import threading
from pathlib import Path

# Re-raised as a plain ImportError (NOT ModuleNotFoundError) on purpose: the
# Database modules' try/except ModuleNotFoundError dual-import blocks would
# otherwise swallow a genuinely missing dependency and fall into their
# bare-import branches, burying this actionable message under a cascade of
# unrelated "No module named 'db'/'Formatters'" errors.
try:
    from cryptography.fernet import Fernet, InvalidToken
except ModuleNotFoundError as e:
    raise ImportError(
        "The 'cryptography' package is missing - it was added as a dependency for "
        "encrypting stored Spotify sessions at rest. Install it with: "
        "pip install -r requirements.txt"
    ) from e

logger = logging.getLogger(__name__)

ENCRYPTION_KEY_ENV_VAR = "DATA_ENCRYPTION_KEY"
FLASK_SECRET_KEY_ENV_VAR = "FLASK_SECRET_KEY"
ENCRYPTED_PREFIX = "enc:v1:"   #< version-tagged so a future scheme change can coexist with old rows
KEY_FILE_NUM_BYTES = 32        #< entropy of an auto-generated key file
DEFAULT_KEY_PATH = Path(__file__).resolve().parent.parent / "secrets" / "data_encryption_key.txt"

# Serializes lazy creation of the key file - two threads encrypting for the
# first time simultaneously must not each generate (and write) their own key.
_keyFileLock = threading.Lock()


def _keyMaterial() -> str:
    envKey = os.environ.get(ENCRYPTION_KEY_ENV_VAR, "").strip()
    if envKey:
        return envKey
    flaskKey = os.environ.get(FLASK_SECRET_KEY_ENV_VAR, "").strip()
    if flaskKey:
        return flaskKey

    with _keyFileLock:
        if DEFAULT_KEY_PATH.exists():
            existing = DEFAULT_KEY_PATH.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        newKey = secrets.token_hex(KEY_FILE_NUM_BYTES)
        DEFAULT_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        DEFAULT_KEY_PATH.write_text(newKey, encoding="utf-8")
        logger.info("Generated a new data encryption key at %s - keep it with your backups: "
                    "without it, stored Spotify sessions can't be read and every user must re-login.",
                    DEFAULT_KEY_PATH)
        return newKey


def _fernet() -> Fernet:
    digest = hashlib.sha256(_keyMaterial().encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def isEncrypted(stored) -> bool:
    return isinstance(stored, str) and stored.startswith(ENCRYPTED_PREFIX)


def encryptSecret(plaintext: str) -> str:
    return ENCRYPTED_PREFIX + _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decryptSecret(stored: str | None) -> str | None:
    """The plaintext for a stored value: encrypted values are decrypted,
    un-prefixed values pass through as legacy plaintext, and None/undecryptable
    values read as None (missing)."""
    if stored is None:
        return None
    if not isEncrypted(stored):
        return stored
    try:
        return _fernet().decrypt(stored[len(ENCRYPTED_PREFIX):].encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        logger.warning(
            "Could not decrypt a stored secret - the encryption key has changed since it was "
            "written (DATA_ENCRYPTION_KEY/FLASK_SECRET_KEY changed, or secrets/data_encryption_key.txt "
            "was lost). Treating it as missing; the affected user must log in again."
        )
        return None
