"""Shared-secret auth for write endpoints exposed beyond the scheduler
itself (spec: "No auth beyond a shared secret"). Header, not query param,
so it doesn't end up in logs/browser history. Fails closed (401) whenever
PWA_SHARED_SECRET is unset -- an empty secret must never mean "open"."""
from fastapi import Header, HTTPException

from .config import get_settings


def require_shared_secret(x_shared_secret: str | None = Header(default=None)) -> None:
    expected = get_settings().pwa_shared_secret
    if not expected or x_shared_secret != expected:
        raise HTTPException(status_code=401, detail="unauthorized")
