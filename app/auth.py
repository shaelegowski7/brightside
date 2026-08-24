"""Shared-secret auth for write endpoints exposed beyond the scheduler
itself (spec: "No auth beyond a shared secret"). Header, not query param,
so it doesn't end up in logs/browser history. Fails closed (401) whenever
PWA_SHARED_SECRET is unset -- an empty secret must never mean "open".
Comparison uses hmac.compare_digest -- a plain `!=` leaks timing
information proportional to the matching prefix length, and this secret
gates real financial data (buy prices, ROI) plus write endpoints."""
import hmac

from fastapi import Header, HTTPException

from .config import get_settings


def require_shared_secret(x_shared_secret: str | None = Header(default=None)) -> None:
    expected = get_settings().pwa_shared_secret
    if not expected or not x_shared_secret or not hmac.compare_digest(x_shared_secret, expected):
        raise HTTPException(status_code=401, detail="unauthorized")
