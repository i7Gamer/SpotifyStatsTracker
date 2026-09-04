# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

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
import functools
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
# The value the README's compose example carries on its commented-out
# DATA_ENCRYPTION_KEY line. Uncommenting that line without editing it encrypts
# every stored session and API secret under a string published in this repo -
# and since this encryption only ever protects a database file that has LEFT
# the host, a publicly-known key means it protects nothing at all. _keyMaterial
# refuses to start on this exact value; app.py's __init__ calls
# keyFingerprint() (which resolves through _keyMaterial) right after
# _get_or_create_secret_key() - its own guard against
# PLACEHOLDER_FLASK_SECRET_KEY (config.py) - so both placeholders now refuse
# construction the same way. That call was added 2026-09-04 (F-B-1): before
# it, this guard was reachable only through a boot-time probe swallowed by
# `except Exception: logger.debug(...)`, so the refusal never actually
# stopped the app from starting.
#
# Kept here rather than beside its twin in config.py because this module owns
# every other key-resolution name (both env vars above, DEFAULT_KEY_PATH) and
# is deliberately import-light - app.py already reads FLASK_SECRET_KEY_ENV_VAR
# from here rather than the other way round.
PLACEHOLDER_DATA_ENCRYPTION_KEY = "changeme-another-random-value"
ENCRYPTED_PREFIX = "enc:v1:"   #< version-tagged so a future scheme change can coexist with old rows
# v2 = v1 plus a short fingerprint of the key that wrote the value:
#   enc:v2:<fingerprint>:<fernet token>
# Without it, "encrypted under a different key" and "corrupt" are the same
# observation - decryptSecret returns None for both, and every caller reads None
# as "no credentials stored". So restoring a database backup without its
# matching secrets/ key file silently logs every user out and strands their
# refresh tokens, with nothing anywhere saying why. The two states want opposite
# responses (find the old key vs. re-authenticate), so the value has to say
# which one it is.
#
# The fingerprint is a truncated hash of the key material, never the key. It
# discloses nothing to an attacker holding the database, who can already test a
# candidate key by attempting to decrypt.
ENCRYPTED_PREFIX_V2 = "enc:v2:"
KEY_FINGERPRINT_LENGTH = 8
KEY_FILE_NUM_BYTES = 32        #< entropy of an auto-generated key file
DEFAULT_KEY_PATH = Path(__file__).resolve().parent.parent / "secrets" / "data_encryption_key.txt"
# A key file readable by every account on the host is one `cat` away from
# forged sessions and decrypted Spotify credentials. This is NOT a defence
# against the host being compromised - the app reads the key unattended at
# boot, so whatever it can read as its own user, an attacker running as that
# user can read too. It defends against the mundane cases: another local
# account, an over-broad share ACL, and the key riding along in an archive or
# a `docker cp` of the install directory.
KEY_FILE_MODE = 0o600          #< owner read/write, nobody else
SECRETS_DIR_MODE = 0o700       #< and nobody else may even list the directory
PARTIAL_SUFFIX = ".partial"    #< the temp name a key file is written under before the rename

# Serializes lazy creation of the key file - two threads encrypting for the
# first time simultaneously must not each generate (and write) their own key.
_keyFileLock = threading.Lock()

# CPython's os.chmod on Windows only toggles FILE_ATTRIBUTE_READONLY; it never
# touches the ACL, and st_mode comes back synthetic (0o666/0o444). Calling it
# there would be a no-op dressed up as a fix, and the "is this looser than we
# want" check below would try to repair every file on every boot. Restricting a
# Windows install means an ACL (icacls / SetNamedSecurityInfo); that is
# deliberately not attempted here rather than pretending a 0o600 did something.
_CAN_SET_FILE_MODE = os.name != "nt"


def _restrictPermissions(path: Path, mode: int) -> None:
    """Narrow `path` to `mode` if it currently grants more than that.

    Only ever narrows: a file someone deliberately set to 0o400 is left as it
    is. Failure is logged and survived - a key file owned by another account
    still has to be readable, and the app going down is worse than the
    permissions staying loose."""
    if not _CAN_SET_FILE_MODE:
        return
    try:
        currentMode = path.stat().st_mode & 0o777
    except OSError:
        return
    if not currentMode & ~mode:
        return
    try:
        os.chmod(path, mode)
    except OSError as e:
        logger.warning("Could not restrict permissions on %s (%s) - it stays readable by other "
                       "accounts on this host.", path, e)
        return
    logger.info("Tightened permissions on %s from %o to %o.", path, currentMode, mode)


def _writeKeyFile(path: Path, contents: str) -> None:
    """Persist a freshly-minted key, atomically and owner-only from the moment
    it exists.

    Atomic because a plain write truncates first, so a crash or a full disk
    between the two leaves exactly the empty file readOrCreateKeyFile has to
    refuse. Owner-only at creation rather than chmod'ed afterwards, because
    chmod-after leaves a window in which the key is world-readable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _restrictPermissions(path.parent, SECRETS_DIR_MODE)
    tempPath = path.with_name(path.name + PARTIAL_SUFFIX)
    # A .partial left behind by a killed process would make O_EXCL fail forever,
    # and reusing it would keep its old (possibly loose) mode - os.open applies
    # the mode only when it creates the file.
    tempPath.unlink(missing_ok=True)
    descriptor = os.open(tempPath, os.O_WRONLY | os.O_CREAT | os.O_EXCL, KEY_FILE_MODE)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(contents)
        os.replace(tempPath, path)
    except Exception:
        # Swallowed on purpose: this runs inside an `except`, where a raised
        # exception REPLACES the one being handled. A boot that cannot persist
        # its key is diagnosed entirely from the message the operator sees, and
        # on Windows the moment a file has just been written is exactly when a
        # scanner may still hold it - "No space left on device" turning into a
        # delete permission error points at the wrong fix. The leftover does not
        # wedge the next boot either: the unlink at the top of this function
        # clears a stale .partial before O_EXCL ever sees it.
        try:
            tempPath.unlink(missing_ok=True)
        except OSError as cleanupError:
            logger.warning("Could not remove the incomplete key file %s: %s", tempPath, cleanupError)
        raise


def readOrCreateKeyFile(path: Path, *, emptyFileError: str | None = None,
                        createdMessage: str | None = None) -> str:
    """The key stored at `path`, minting and persisting a random one if it is
    not there yet.

    `emptyFileError` decides what an existing-but-EMPTY file means, which is
    the only way the two callers differ. The data encryption key passes one:
    minting over it would make every enc: value in the database undecryptable,
    which decryptSecret reports as None, which every caller reads as "no
    credentials stored" - so every user is silently bounced to re-login and
    their Spotify refresh tokens are unrecoverable. Restoring the file from a
    backup fixes that; minting cannot be undone, so it has to refuse. The Flask
    session key passes None: losing it only invalidates live sessions, so an
    empty file is just a failed write to redo."""
    with _keyFileLock:
        if path.exists():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                # Also on the read path, not just at creation: an install that
                # predates this wrote the file with the default mode and will
                # never re-create it, so a mint-only fix would never reach the
                # files that are actually exposed.
                _restrictPermissions(path, KEY_FILE_MODE)
                _restrictPermissions(path.parent, SECRETS_DIR_MODE)
                return existing
            if emptyFileError:
                raise RuntimeError(emptyFileError)
            logger.warning("The key file at %s was empty - generating a new one.", path)
        newKey = secrets.token_hex(KEY_FILE_NUM_BYTES)
        _writeKeyFile(path, newKey)
        if createdMessage:
            logger.info(createdMessage)
        return newKey


# The SHA-256 derivations below are cached on the key MATERIAL, not on "the
# current key": _keyMaterial is still consulted every time, so an env var or key
# file changing at runtime is still honoured. This only avoids re-hashing the
# same string, which mattered because isForeignKeyed + _fernet each derived
# independently - so every decryptSecret hashed twice, and
# countSecretsUnderAnotherKey did it once per secret column per user.
# Small: only the env key, the file key, and a test's substitutes ever appear.
_DERIVATION_CACHE_SIZE = 4


def _keyMaterial() -> str:
    envKey = os.environ.get(ENCRYPTION_KEY_ENV_VAR, "").strip()
    if envKey:
        if envKey == PLACEHOLDER_DATA_ENCRYPTION_KEY:
            raise RuntimeError(
                f"{ENCRYPTION_KEY_ENV_VAR} is still set to the docker-compose placeholder "
                "value. It is published in this repository, so every stored Spotify session "
                "and API secret would be encrypted under a key anyone can read. Generate a "
                "real random key - e.g. `python -c \"import secrets; print(secrets.token_hex(32))\"` "
                f"- and set {ENCRYPTION_KEY_ENV_VAR} to it before starting, or comment the "
                "variable out to fall back to " + FLASK_SECRET_KEY_ENV_VAR + "."
            )
        return envKey
    flaskKey = os.environ.get(FLASK_SECRET_KEY_ENV_VAR, "").strip()
    if flaskKey:
        return flaskKey

    # An existing-but-EMPTY key file is not "no key yet" here - see
    # readOrCreateKeyFile, which refuses rather than minting over it.
    return readOrCreateKeyFile(
        DEFAULT_KEY_PATH,
        emptyFileError=(
            f"The data encryption key file at {DEFAULT_KEY_PATH} exists but is empty. "
            "Restore it from a backup - without it, every stored Spotify session and API "
            "credential is unreadable. If this really is a fresh install, delete the empty "
            f"file and restart, or set {ENCRYPTION_KEY_ENV_VAR}."
        ),
        createdMessage=(
            f"Generated a new data encryption key at {DEFAULT_KEY_PATH} - keep it with your "
            "backups: without it, stored Spotify sessions can't be read and every user must "
            "re-login."
        ),
    )


@functools.lru_cache(maxsize=_DERIVATION_CACHE_SIZE)
def _fernetFor(material: str) -> Fernet:
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


@functools.lru_cache(maxsize=_DERIVATION_CACHE_SIZE)
def _fingerprintFor(material: str) -> str:
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:KEY_FINGERPRINT_LENGTH]


def _fernet() -> Fernet:
    return _fernetFor(_keyMaterial())


def keyFingerprint() -> str:
    """A short, non-reversible tag for the key currently in use."""
    return _fingerprintFor(_keyMaterial())


def isEncrypted(stored) -> bool:
    return isinstance(stored, str) and stored.startswith((ENCRYPTED_PREFIX, ENCRYPTED_PREFIX_V2))


def _storedFingerprint(stored) -> str | None:
    """The fingerprint a v2 value carries, or None for anything else (a v1
    value, plaintext, or a malformed one) - the key that wrote those is simply
    not knowable."""
    if not isinstance(stored, str) or not stored.startswith(ENCRYPTED_PREFIX_V2):
        return None
    remainder = stored[len(ENCRYPTED_PREFIX_V2):]
    fingerprint, separator, _ = remainder.partition(":")
    return fingerprint if separator else None


def isForeignKeyed(stored, currentFingerprint: str | None = None) -> bool:
    """True when this value names a key that is NOT the one in use - i.e. it is
    readable again if the right key comes back, as opposed to being damaged.
    False for anything that cannot be classified, so nothing is ever claimed
    about a v1 value.

    `currentFingerprint` lets a caller classifying MANY values resolve the key
    once instead of per value (see countSecretsUnderAnotherKey, which otherwise
    re-read the key file for every secret column of every user, each time behind
    _keyFileLock)."""
    fingerprint = _storedFingerprint(stored)
    if fingerprint is None:
        return False
    return fingerprint != (currentFingerprint if currentFingerprint is not None else keyFingerprint())


def encryptSecret(plaintext: str) -> str:
    token = _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")
    return f"{ENCRYPTED_PREFIX_V2}{keyFingerprint()}:{token}"


def decryptSecret(stored: str | None) -> str | None:
    """The plaintext for a stored value: encrypted values are decrypted,
    un-prefixed values pass through as legacy plaintext, and None/undecryptable
    values read as None (missing)."""
    if stored is None:
        return None
    if not isEncrypted(stored):
        return stored
    if isForeignKeyed(stored):
        # Said plainly, because it is recoverable and the recovery is a
        # different action from re-authenticating: the value is intact, it just
        # belongs to another key. This is what a database restored without its
        # matching secrets/ file looks like.
        logger.warning(
            "A stored secret was encrypted with a different key (it names %s, this instance uses "
            "%s) - most likely a database restored without its matching "
            "secrets/data_encryption_key.txt, or DATA_ENCRYPTION_KEY/FLASK_SECRET_KEY changed. "
            "Restore the original key to recover it; otherwise the affected user must log in again.",
            _storedFingerprint(stored), keyFingerprint(),
        )
        return None
    prefix = ENCRYPTED_PREFIX_V2 if stored.startswith(ENCRYPTED_PREFIX_V2) else ENCRYPTED_PREFIX
    token = stored[len(prefix):]
    if prefix is ENCRYPTED_PREFIX_V2:
        token = token.partition(":")[2]   #< strip the fingerprint this value carries
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        logger.warning(
            "Could not decrypt a stored secret - the encryption key has changed since it was "
            "written (DATA_ENCRYPTION_KEY/FLASK_SECRET_KEY changed, or secrets/data_encryption_key.txt "
            "was lost), or the value is damaged. Treating it as missing; the affected user must "
            "log in again."
        )
        return None
