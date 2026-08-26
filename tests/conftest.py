"""Sets required env vars to a throwaway sqlite DB before any `app.*` module
is imported (app.database builds its engine at import time), so the test
suite never touches Postgres or needs real secrets."""
import base64
import json
import os
import tempfile

_tmp_dir = tempfile.mkdtemp(prefix="fba_test_")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp_dir}/test.db")
os.environ.setdefault("KEEPA_API_KEY", "test")
os.environ.setdefault("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/test/test")
os.environ.setdefault("PWA_ORIGIN", "https://pwa.example.com")
# Supabase creds are never actually used over the network in tests --
# app.auth._verify_token is monkeypatched below -- but config.py's
# Settings loads them as hard-required at import time, same tier as
# DATABASE_URL, so *something* has to be present.
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-key")
os.environ.setdefault("ALLOWED_USER_EMAILS", "test@example.com")

import pytest  # noqa: E402

from app.database import Base, SessionLocal, engine  # noqa: E402
from app import auth, auth_monitor, models  # noqa: E402,F401

Base.metadata.create_all(bind=engine)

TEST_USER_EMAIL = "test@example.com"


def fake_jwt(aal: str = "aal2") -> str:
    """Builds a well-formed-enough JWT for auth._extract_aal to decode --
    a real base64url-encoded {"aal": ...} payload as the middle segment.
    Header/signature segments are arbitrary placeholders: _extract_aal
    never validates them, only decodes the payload (see auth.py's
    docstring for why that's safe -- it only ever runs after
    _verify_token has already proven the token genuine, and in tests
    _verify_token itself is stubbed below, not this token's signature)."""
    payload = base64.urlsafe_b64encode(json.dumps({"aal": aal}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


AUTH_HEADERS = {"Authorization": f"Bearer {fake_jwt('aal2')}"}


@pytest.fixture(autouse=True)
def _reset_auth_monitor():
    """auth_monitor's failed-attempt tracker is module-level global state
    (see its own docstring) -- several smoke tests deliberately trigger a
    401 via the auth paths it watches. Without resetting between tests,
    those failures would accumulate across the whole suite and eventually
    cross the alert threshold, firing a real network call to
    DISCORD_WEBHOOK_URL's fake test value above."""
    auth_monitor._reset_for_tests()
    yield
    auth_monitor._reset_for_tests()


@pytest.fixture(autouse=True)
def _stub_verify_token(monkeypatch):
    """Stubs the one function in the whole suite that would otherwise hit
    a real Supabase project (see auth.py's docstring: _verify_token is
    deliberately the sole network-touching seam). Returns a fixed allow-
    listed user for any token by default -- individual tests override this
    per-test (monkeypatch.setattr(auth, "_verify_token", ...)) to exercise
    the invalid-token/non-allowlisted-email paths in tests/test_auth.py."""
    monkeypatch.setattr(auth, "_verify_token", lambda token: auth.AuthedUser(id="test-user-id", email=TEST_USER_EMAIL))


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(table.delete())
        session.commit()
        session.close()
