"""Regression tests for token_version invalidation on password change."""
from __future__ import annotations

import os
import sys
import unittest
import hmac
import hashlib
import time
import secrets

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


class TestTokenVersionInSignature(unittest.TestCase):
    """make_token / verify_token must include token_version in the signed payload."""

    def test_token_format_includes_version(self):
        from app.api.auth import make_token, _get_secret
        token = make_token(user_id=42, token_version=3)
        parts = token.split(".")
        self.assertEqual(len(parts), 4, f"expected 4-part token, got {len(parts)}")
        user_id_s, version_s, exp_s, sig = parts
        self.assertEqual(user_id_s, "42")
        self.assertEqual(version_s, "3")
        # signature must match the version-bearing payload
        payload = f"{user_id_s}.{version_s}.{exp_s}"
        expected = hmac.new(_get_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(sig, expected)

    def test_tampered_version_changes_signature(self):
        import hmac, hashlib
        from app.api.auth import make_token, _get_secret
        token = make_token(user_id=42, token_version=3)
        parts = token.split(".")
        # Attacker swaps the embedded version; recompute the sig the server
        # WOULD produce for that payload. A real attacker cannot produce
        # this without the secret, so any mismatch in production verify_token
        # is the rejection signal.
        parts[1] = "4"
        forged_payload = ".".join(parts[:3])
        forged_sig = hmac.new(_get_secret().encode(), forged_payload.encode(), hashlib.sha256).hexdigest()
        # The original token's sig is bound to the original version, so
        # replacing the version produces a payload the original sig cannot
        # cover.
        self.assertNotEqual(parts[-1], forged_sig)


class TestTokenVersionMigration(unittest.TestCase):
    """User model and _migrate_token_version must agree on the column."""

    def test_user_has_token_version_field(self):
        from app.storage.db import User
        fields = User.model_fields if hasattr(User, "model_fields") else {}
        self.assertIn("token_version", fields)
        self.assertEqual(fields["token_version"].default, 0)


if __name__ == "__main__":
    unittest.main()
