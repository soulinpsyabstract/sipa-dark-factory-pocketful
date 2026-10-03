"""Password hashing and bearer tokens.

Spec section 1 requires bcrypt / scrypt / Argon2 - this uses bcrypt.

The password is pre-hashed with SHA-256 and base64'd before bcrypt sees it.
That is the standard fix for bcrypt's silent 72-byte truncation: it keeps every
byte of a long passphrase significant instead of ignoring the tail. The
resulting digest is still a real salted bcrypt hash, and no plaintext is kept
anywhere.
"""

from __future__ import annotations

import base64
import hashlib
import secrets

import bcrypt

MAX_PASSWORD_BYTES = 1024


def _prehash(password: str) -> bytes:
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(password: str) -> str:
    """Return a ``$2b$`` bcrypt digest of ``password``."""
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, stored_hash: str) -> bool:
    """Constant-time check. Returns False rather than raising on a bad hash."""
    if not isinstance(stored_hash, str) or not stored_hash:
        return False
    try:
        return bcrypt.checkpw(_prehash(password), stored_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False


def new_token() -> str:
    return secrets.token_urlsafe(32)
