from __future__ import annotations

import hashlib
import hmac
import json
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from typing import Any


def _b64e(data: bytes) -> str:
    return urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64d(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return urlsafe_b64decode(data + pad)


def sign_session(secret: str, username: str, ttl_hours: int) -> str:
    payload = {
        "u": username,
        "exp": int(time.time()) + max(1, ttl_hours) * 3600,
    }
    body = _b64e(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    sig = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def read_session(secret: str, token: str) -> dict[str, Any] | None:
    if not secret or not token or "." not in token:
        return None
    body, _, sig = token.partition(".")
    expect = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expect, sig):
        return None
    try:
        payload = json.loads(_b64d(body).decode("utf-8"))
    except Exception:
        return None
    if int(payload.get("exp") or 0) < int(time.time()):
        return None
    user = str(payload.get("u") or "").strip()
    if not user:
        return None
    return {"username": user, "exp": int(payload["exp"])}
