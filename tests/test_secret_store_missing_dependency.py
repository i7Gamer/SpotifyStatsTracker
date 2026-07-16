"""A missing 'cryptography' package must fail loudly and actionably.

The Database modules use try/except ModuleNotFoundError dual-import blocks
(package vs. bare imports). When secret_store raised ModuleNotFoundError for
a genuinely missing third-party dependency, those blocks swallowed it and
fell into their bare-import branches, burying the real cause under a cascade
of "No module named 'db'" / "No module named 'Formatters'" errors (seen on a
live instance that hadn't run pip install after the cryptography dependency
was added). secret_store therefore re-raises as a plain ImportError - which
no dual-import block catches - with instructions.
"""
import builtins
import importlib
import sys
import os
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import Database.secret_store as secretStore


class TestMissingCryptographyDependency(unittest.TestCase):
    def test_missing_cryptography_raises_actionable_plain_import_error(self):
        realImport = builtins.__import__

        def importWithoutCryptography(name, *args, **kwargs):
            if name == "cryptography" or name.startswith("cryptography."):
                raise ModuleNotFoundError(f"No module named '{name}'", name=name)
            return realImport(name, *args, **kwargs)

        try:
            with patch.object(builtins, "__import__", side_effect=importWithoutCryptography):
                with self.assertRaises(ImportError) as ctx:
                    importlib.reload(secretStore)

            self.assertNotIsInstance(
                ctx.exception, ModuleNotFoundError,
                "must be a plain ImportError so the Database modules' "
                "'except ModuleNotFoundError' dual-import fallbacks don't swallow it")
            self.assertIn("cryptography", str(ctx.exception))
            self.assertIn("pip install -r requirements.txt", str(ctx.exception))
        finally:
            importlib.reload(secretStore)   #< restore the real module for every later test


if __name__ == "__main__":
    unittest.main()
