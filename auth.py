"""Admin authentication via the SSO server's /auth/signin endpoint.

Flow (signin-only, as requested):
  1. Admin submits email + password to MovieHouse.
  2. We call <SSO_BASE_URL>/auth/signin with HTTP Basic auth.
  3. If the SSO returns status == "OK", we create a LOCAL session and hand the
     browser an HttpOnly cookie. We never store the password, and we don't call
     verify/refresh — the local session is the source of truth after login.
"""
from __future__ import annotations

import base64
import secrets
import time

import httpx

import config

# In-memory session store: sid -> {"email": str, "expires": float}.
# Cleared on restart (fine for this use case).
_sessions: dict[str, dict] = {}


async def sso_signin(email: str, password: str) -> dict:
    """Call the SSO signin endpoint. Returns the parsed JSON (SessionResponse).

    On success: {"status": "OK", "accessToken": {...}, ...}
    On failure: {"status": "WRONG_CREDENTIALS_ERROR"}
    """
    token = base64.b64encode(f"{email}:{password}".encode()).decode()
    url = f"{config.SSO_BASE_URL}/auth/signin"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, headers={"Authorization": f"Basic {token}"})
    try:
        return resp.json()
    except Exception:
        return {"status": "ERROR", "detail": f"HTTP {resp.status_code}"}


def email_allowed(email: str) -> bool:
    if not config.ADMIN_EMAILS:
        return True
    return email.lower() in config.ADMIN_EMAILS


def create_session(email: str) -> str:
    sid = secrets.token_urlsafe(32)
    _sessions[sid] = {"email": email, "expires": time.time() + config.SESSION_TTL}
    return sid


def session_email(sid: str | None) -> str | None:
    """Return the email for a valid, non-expired session, else None."""
    if not sid:
        return None
    s = _sessions.get(sid)
    if not s:
        return None
    if s["expires"] < time.time():
        _sessions.pop(sid, None)
        return None
    return s["email"]


def destroy_session(sid: str | None) -> None:
    if sid:
        _sessions.pop(sid, None)
