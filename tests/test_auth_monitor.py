from app import auth_monitor, discord_notifier

# conftest.py's autouse _reset_auth_monitor fixture resets auth_monitor's
# module-level state before/after every test in the suite, this file
# included -- no local reset fixture needed here.


def test_no_alert_below_threshold(monkeypatch):
    send_calls = []
    monkeypatch.setattr(discord_notifier, "send_ping", lambda url, embed: send_calls.append((url, embed)) or True)

    for _ in range(auth_monitor._THRESHOLD - 1):
        auth_monitor.record_failed_attempt("test_source")

    assert send_calls == []


def test_alert_fires_at_threshold(monkeypatch):
    send_calls = []
    monkeypatch.setattr(discord_notifier, "send_ping", lambda url, embed: send_calls.append((url, embed)) or True)

    for _ in range(auth_monitor._THRESHOLD):
        auth_monitor.record_failed_attempt("test_source")

    assert len(send_calls) == 1
    _, embed = send_calls[0]
    assert embed["title"] == "Repeated failed shared-secret attempts"
    assert any(f["name"] == "Last source" and f["value"] == "test_source" for f in embed["fields"])


def test_alert_has_cooldown_and_does_not_spam(monkeypatch):
    send_calls = []
    monkeypatch.setattr(discord_notifier, "send_ping", lambda url, embed: send_calls.append((url, embed)) or True)

    for _ in range(auth_monitor._THRESHOLD + 10):
        auth_monitor.record_failed_attempt("test_source")

    assert len(send_calls) == 1


def test_old_attempts_outside_window_are_dropped(monkeypatch):
    from datetime import datetime, timedelta, timezone

    send_calls = []
    monkeypatch.setattr(discord_notifier, "send_ping", lambda url, embed: send_calls.append((url, embed)) or True)

    old = datetime.now(timezone.utc) - auth_monitor._WINDOW - timedelta(minutes=5)
    auth_monitor._failures.extend([old] * (auth_monitor._THRESHOLD - 1))

    auth_monitor.record_failed_attempt("test_source")

    # the stale entries should have been pruned, leaving just this one attempt
    assert len(auth_monitor._failures) == 1
    assert send_calls == []
