"""Signing keys and access tokens for built-in auth."""

import uuid
from datetime import UTC, datetime, timedelta
from functools import cache

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from . import config

_PRIVATE = "jwt_private.pem"
_PUBLIC = "jwt_public.pem"


@cache
def _keys() -> tuple[bytes, bytes]:
    """Load the RSA key pair, creating it on first use."""
    key_dir = config.AUTH_KEY_DIR
    private_path, public_path = key_dir / _PRIVATE, key_dir / _PUBLIC
    if not private_path.exists():
        key_dir.mkdir(parents=True, exist_ok=True)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
        public_path.write_bytes(key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ))
    return private_path.read_bytes(), public_path.read_bytes()


def public_key_pem() -> str:
    return _keys()[1].decode()


def issue_token(user_id: str, token_version: int, ttl_hours: int | None = None) -> tuple[str, int]:
    """Return a signed access token for the user and its lifetime in seconds."""
    ttl = timedelta(hours=ttl_hours or config.TOKEN_TTL_HOURS)
    now = datetime.now(UTC)
    payload = {
        "sub": user_id,
        "iss": config.TOKEN_ISSUER,
        "aud": config.TOKEN_AUDIENCE,
        "iat": now,
        "exp": now + ttl,
        "jti": uuid.uuid4().hex,
        "ver": token_version,
    }
    token = jwt.encode(payload, _keys()[0], algorithm="RS256")
    return token, int(ttl.total_seconds())
