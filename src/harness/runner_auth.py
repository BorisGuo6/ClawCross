"""Runner transport token helpers for the harness control plane."""

from __future__ import annotations

import hashlib
import hmac
import secrets


RUNNER_TOKEN_HASH_PREFIX = "sha256:"


def generate_runner_token() -> str:
    return secrets.token_urlsafe(32)


def hash_runner_token(token: str) -> str:
    clean = str(token or "").strip()
    if not clean:
        return ""
    digest = hashlib.sha256(clean.encode("utf-8")).hexdigest()
    return f"{RUNNER_TOKEN_HASH_PREFIX}{digest}"


def verify_runner_token_hash(token: str, stored_hash: str) -> bool:
    clean_hash = str(stored_hash or "").strip()
    if not clean_hash.startswith(RUNNER_TOKEN_HASH_PREFIX):
        return False
    return hmac.compare_digest(hash_runner_token(token), clean_hash)
