"""Sets required env vars to a throwaway sqlite DB before any `app.*` module
is imported (app.database builds its engine at import time), so the test
suite never touches Postgres or needs real secrets."""
import os
import tempfile

_tmp_dir = tempfile.mkdtemp(prefix="fba_test_")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp_dir}/test.db")
os.environ.setdefault("KEEPA_API_KEY", "test")
os.environ.setdefault("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/test/test")
os.environ.setdefault("PWA_SHARED_SECRET", "test-shared-secret")
os.environ.setdefault("PWA_ORIGIN", "https://pwa.example.com")

import pytest  # noqa: E402

from app.database import Base, SessionLocal, engine  # noqa: E402
from app import auth_monitor, models  # noqa: E402,F401

Base.metadata.create_all(bind=engine)


@pytest.fixture(autouse=True)
def _reset_auth_monitor():
    """auth_monitor's failed-attempt tracker is module-level global state
    (see its own docstring) -- several smoke tests deliberately trigger a
    401 via the shared-secret paths it watches. Without resetting between
    tests, those failures would accumulate across the whole suite and
    eventually cross the alert threshold, firing a real network call to
    DISCORD_WEBHOOK_URL's fake test value above."""
    auth_monitor._reset_for_tests()
    yield
    auth_monitor._reset_for_tests()


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
