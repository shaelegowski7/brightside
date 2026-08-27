"""Isolation tests for auth.py's layers that conftest's autouse
_stub_verify_token fixture doesn't otherwise exercise: the pure AAL decode,
and the allowlist/aal-elevation checks inside authenticate()/require_user.
Each test that needs a specific _verify_token behavior overrides the
conftest-level happy-path stub for just that test."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app import auth, auth_monitor, discord_notifier
from tests.conftest import TEST_USER_EMAIL, fake_jwt

# --- _extract_aal: pure, no mocking needed ---


def test_extract_aal_reads_aal2():
    assert auth._extract_aal(fake_jwt("aal2")) == "aal2"


def test_extract_aal_reads_aal1():
    assert auth._extract_aal(fake_jwt("aal1")) == "aal1"


def test_extract_aal_returns_none_for_malformed_token():
    assert auth._extract_aal("not-a-jwt") is None
    assert auth._extract_aal("") is None


# --- authenticate()/require_user: allowlist + aal checks ---


def test_require_user_rejects_missing_token():
    with pytest.raises(HTTPException) as exc:
        auth.require_user(authorization=None)
    assert exc.value.status_code == 401


def test_require_user_does_not_record_failed_attempt_on_missing_token(monkeypatch):
    send_calls = []
    monkeypatch.setattr(discord_notifier, "send_ping", lambda url, embed: send_calls.append(1) or True)

    for _ in range(10):
        try:
            auth.require_user(authorization=None)
        except HTTPException:
            pass

    assert send_calls == []


def test_require_user_rejects_invalid_token(monkeypatch):
    def _raise(token):
        raise RuntimeError("invalid token")
    monkeypatch.setattr(auth, "_verify_token", _raise)

    with pytest.raises(HTTPException) as exc:
        auth.require_user(authorization=f"Bearer {fake_jwt('aal2')}")
    assert exc.value.status_code == 401


def test_require_user_records_failed_attempt_on_invalid_token(monkeypatch):
    def _raise(token):
        raise RuntimeError("invalid token")
    monkeypatch.setattr(auth, "_verify_token", _raise)

    send_calls = []
    monkeypatch.setattr(discord_notifier, "send_ping", lambda url, embed: send_calls.append(embed) or True)

    for _ in range(auth_monitor._THRESHOLD):
        try:
            auth.require_user(authorization=f"Bearer {fake_jwt('aal2')}")
        except HTTPException:
            pass

    assert len(send_calls) == 1
    assert any(f["value"] == "invalid_token" for f in send_calls[0]["fields"] if f["name"] == "Last source")


def test_require_user_rejects_non_allowlisted_email(monkeypatch):
    monkeypatch.setattr(auth, "_verify_token", lambda token: auth.AuthedUser(id="x", email="stranger@example.com"))

    with pytest.raises(HTTPException) as exc:
        auth.require_user(authorization=f"Bearer {fake_jwt('aal2')}")
    assert exc.value.status_code == 403


def test_require_user_allowlist_check_is_case_insensitive(monkeypatch):
    monkeypatch.setattr(auth, "_verify_token", lambda token: auth.AuthedUser(id="x", email=TEST_USER_EMAIL.upper()))

    # Should pass -- no exception -- since the allowlist compares lowercased.
    user = auth.require_user(authorization=f"Bearer {fake_jwt('aal2')}")
    assert user.email == TEST_USER_EMAIL.upper()


def test_require_user_rejects_aal1_session():
    with pytest.raises(HTTPException) as exc:
        auth.require_user(authorization=f"Bearer {fake_jwt('aal1')}")
    assert exc.value.status_code == 401
    assert exc.value.detail == "mfa_required"


def test_require_user_accepts_aal2_session():
    user = auth.require_user(authorization=f"Bearer {fake_jwt('aal2')}")
    assert user.email == TEST_USER_EMAIL


# --- authenticate()/require_user: password rotation ---


def test_require_user_rejects_expired_password(monkeypatch):
    old = datetime.now(timezone.utc) - timedelta(days=400)
    monkeypatch.setattr(
        auth, "_verify_token",
        lambda token: auth.AuthedUser(id="x", email=TEST_USER_EMAIL, password_changed_at=old),
    )

    with pytest.raises(HTTPException) as exc:
        auth.require_user(authorization=f"Bearer {fake_jwt('aal2')}")
    assert exc.value.status_code == 401
    assert exc.value.detail == "password_expired"


def test_require_user_accepts_recently_changed_password(monkeypatch):
    recent = datetime.now(timezone.utc) - timedelta(days=10)
    monkeypatch.setattr(
        auth, "_verify_token",
        lambda token: auth.AuthedUser(id="x", email=TEST_USER_EMAIL, password_changed_at=recent),
    )

    user = auth.require_user(authorization=f"Bearer {fake_jwt('aal2')}")
    assert user.email == TEST_USER_EMAIL


def test_require_user_falls_back_to_created_at_when_never_changed(monkeypatch):
    # No password_changed_at at all (account has never gone through
    # ChangePassword) -- age is measured from account creation instead.
    old = datetime.now(timezone.utc) - timedelta(days=400)
    recent = datetime.now(timezone.utc) - timedelta(days=10)

    monkeypatch.setattr(
        auth, "_verify_token",
        lambda token: auth.AuthedUser(id="x", email=TEST_USER_EMAIL, password_changed_at=None, created_at=old),
    )
    with pytest.raises(HTTPException) as exc:
        auth.require_user(authorization=f"Bearer {fake_jwt('aal2')}")
    assert exc.value.detail == "password_expired"

    monkeypatch.setattr(
        auth, "_verify_token",
        lambda token: auth.AuthedUser(id="x", email=TEST_USER_EMAIL, password_changed_at=None, created_at=recent),
    )
    user = auth.require_user(authorization=f"Bearer {fake_jwt('aal2')}")
    assert user.email == TEST_USER_EMAIL


def test_require_user_records_failed_attempt_on_password_expired(monkeypatch):
    old = datetime.now(timezone.utc) - timedelta(days=400)
    monkeypatch.setattr(
        auth, "_verify_token",
        lambda token: auth.AuthedUser(id="x", email=TEST_USER_EMAIL, password_changed_at=old),
    )

    send_calls = []
    monkeypatch.setattr(discord_notifier, "send_ping", lambda url, embed: send_calls.append(embed) or True)

    for _ in range(auth_monitor._THRESHOLD):
        try:
            auth.require_user(authorization=f"Bearer {fake_jwt('aal2')}")
        except HTTPException:
            pass

    assert len(send_calls) == 1
    assert any(f["value"] == "password_expired" for f in send_calls[0]["fields"] if f["name"] == "Last source")
