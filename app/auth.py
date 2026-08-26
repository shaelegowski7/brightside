"""Supabase-backed auth, replacing the old shared-secret model. Every real
user gets their own email/password account with mandatory TOTP MFA -- no
route is ever reachable at aal1 (password-only), matching the PWA's own
AuthGate, which never sends a request until the session reaches aal2 (see
pwa/src/App.tsx). This module is the enforcement backstop for that, not
just the PWA's UX: a client bypassing the PWA entirely with a valid-but-
unelevated token still gets rejected here.

Layered so each piece is independently testable without a real Supabase
project (see tests/test_auth.py) -- only `_verify_token` ever touches the
network; everything above it is pure/local.

AAL (Authenticator Assurance Level) mechanics, confirmed against the
installed supabase-py/gotrue-py source, not just docs: `auth.get_user()`
proves a token is genuine (real network round-trip) but its response has
no `aal` field at all -- that claim only exists inside the JWT payload
itself, and Supabase's own client SDKs read it via a *local* base64 decode
of the token, no signature check (safe only because get_user() already
proved the token genuine). So confirming "this specific session passed
MFA" is unavoidably two steps: verify the token is real (network), then
decode its own payload to read `aal` (no network). We reimplement the
decode locally rather than reach into supabase_auth's private helpers,
since those aren't public API and could shift under us on a version bump.
"""
import base64
import json
from dataclasses import dataclass

from fastapi import Header, HTTPException
from supabase import create_client

from . import auth_monitor
from .config import get_settings


@dataclass
class AuthedUser:
    id: str
    email: str


class AuthError(Exception):
    """Transport-agnostic auth failure -- both the HTTP dependency and the
    crawl_ws websocket catch this and translate it into their own failure
    mode (HTTPException vs. a websocket close code)."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _get_supabase_client():
    s = get_settings()
    return create_client(s.supabase_url, s.supabase_service_key)


def _verify_token(token: str) -> AuthedUser:
    """The one function in this module that touches the network -- tests
    monkeypatch this directly rather than mocking the Supabase client."""
    user = _get_supabase_client().auth.get_user(token).user
    return AuthedUser(id=user.id, email=user.email)


def _extract_aal(token: str) -> str | None:
    """Local-only decode of the JWT payload's `aal` claim -- no network,
    no signature verification (see module docstring for why that's safe
    here). Returns None on anything that doesn't look like a well-formed
    token rather than raising, since this always runs after _verify_token
    has already proven the token genuine; a decode failure at that point
    just means "couldn't read an aal claim," not "invalid token.\""""
    try:
        _, payload_b64, _ = token.split(".")
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        return payload.get("aal")
    except (ValueError, TypeError):
        return None


def _allowlisted_emails() -> set[str]:
    raw = get_settings().allowed_user_emails
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def authenticate(token: str | None) -> AuthedUser:
    """Core check, in order: token presented -> token genuine -> email
    allowlisted -> session is MFA-elevated (aal2). Records a failed-attempt
    signal (see auth_monitor.py) on every rejection except a missing
    token -- presenting no credential at all isn't the same signal as
    presenting one that gets rejected."""
    if not token:
        raise AuthError("missing_token")

    try:
        user = _verify_token(token)
    except Exception:
        auth_monitor.record_failed_attempt("invalid_token")
        raise AuthError("invalid_token")

    if user.email.lower() not in _allowlisted_emails():
        auth_monitor.record_failed_attempt("email_not_allowlisted")
        raise AuthError("not_authorized")

    if _extract_aal(token) != "aal2":
        auth_monitor.record_failed_attempt("insufficient_aal")
        raise AuthError("mfa_required")

    return user


_HTTP_STATUS_BY_REASON = {
    "missing_token": 401,
    "invalid_token": 401,
    "mfa_required": 401,
    # Distinct from the rest -- a 401 makes the PWA clear its session and
    # re-show the login form (see pwa/src/api.ts), which would just
    # succeed again and loop for a valid-but-unauthorized account. 403
    # surfaces its own "not authorized" message instead.
    "not_authorized": 403,
}


def require_user(authorization: str | None = Header(default=None)) -> AuthedUser:
    token = authorization.removeprefix("Bearer ").strip() if authorization else None
    try:
        return authenticate(token)
    except AuthError as e:
        raise HTTPException(status_code=_HTTP_STATUS_BY_REASON[e.reason], detail=e.reason)
