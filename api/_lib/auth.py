"""Shared-password session cookie.

The Streamlit Cloud app relied on Streamlit's own access control. On Vercel the
API is a public URL, so it verifies the same signed cookie the Next.js login
route issues. Both sides sign with DASHBOARD_SECRET.

Not a user system — one shared password, matching how the team already uses the
dashboard. Swap for Vercel Authentication (Pro) or an OIDC provider if per-user
access is ever needed.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time

COOKIE_NAME = "md_session"
TTL_SECONDS = 60 * 60 * 24 * 14   # 14 days


def _secret() -> bytes:
    s = os.getenv("DASHBOARD_SECRET")
    if not s:
        raise RuntimeError("DASHBOARD_SECRET is not set")
    return s.encode()


def issue(now: float | None = None) -> str:
    exp = int((now or time.time()) + TTL_SECONDS)
    payload = base64.urlsafe_b64encode(str(exp).encode()).decode().rstrip("=")
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    payload, _, sig = token.rpartition(".")
    try:
        expected = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    except RuntimeError:
        return False
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        pad = "=" * (-len(payload) % 4)
        exp = int(base64.urlsafe_b64decode(payload + pad).decode())
    except Exception:
        return False
    return exp > time.time()


def check_password(candidate: str) -> bool:
    expected = os.getenv("DASHBOARD_PASSWORD")
    if not expected:
        return False
    return hmac.compare_digest(candidate.encode(), expected.encode())


def auth_disabled() -> bool:
    """Local dev convenience: with no password configured, don't lock the app."""
    return not os.getenv("DASHBOARD_PASSWORD")
