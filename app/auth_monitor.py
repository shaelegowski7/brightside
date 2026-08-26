"""Lightweight failed-auth tracking for the shared-secret gate -- not a
real IDS, but a genuine early-warning signal: a burst of wrong-secret
attempts gets a Discord alert instead of vanishing into request logs
nobody reads. Only counts attempts where a secret was actually supplied
and didn't match -- a missing/unconfigured PWA_SHARED_SECRET is a config
problem, not an attack signal, so that path never reaches here (see
auth.py, main.py's /deals/login and crawl_ws, which all short-circuit on
`not expected` before calling record_failed_attempt).

In-memory only (module-level, like crawl_runner.py's state) -- fine for
this app's single Railway instance (see Procfile); a restart just resets
the window, which only means "one fewer alert on an already-brief burst,"
not a correctness problem."""
import threading
from collections import deque
from datetime import datetime, timedelta, timezone

from . import discord_notifier
from .config import get_settings

_WINDOW = timedelta(minutes=10)
_THRESHOLD = 5
_ALERT_COOLDOWN = timedelta(minutes=30)

_lock = threading.Lock()
_failures: deque[datetime] = deque()
_last_alert: datetime | None = None


def record_failed_attempt(source: str) -> None:
    """Call on every wrong-secret attempt. Fires a Discord alert once the
    window holds >= _THRESHOLD attempts, then stays quiet for
    _ALERT_COOLDOWN even if attempts keep coming -- a sustained burst
    should page once, not once per request."""
    global _last_alert
    now = datetime.now(timezone.utc)
    with _lock:
        _failures.append(now)
        cutoff = now - _WINDOW
        while _failures and _failures[0] < cutoff:
            _failures.popleft()
        count = len(_failures)
        should_alert = count >= _THRESHOLD and (_last_alert is None or now - _last_alert >= _ALERT_COOLDOWN)
        if should_alert:
            _last_alert = now

    if should_alert:
        _send_alert(count, source)


def _send_alert(count: int, source: str) -> None:
    embed = {
        "title": "Repeated failed shared-secret attempts",
        "color": discord_notifier.COLOR_AMBER,
        "fields": [
            {"name": "Attempts", "value": f"{count} in the last {int(_WINDOW.total_seconds() // 60)} minutes", "inline": True},
            {"name": "Last source", "value": source, "inline": True},
        ],
    }
    ok = discord_notifier.send_ping(get_settings().discord_webhook_url, embed)
    print(f"[AUTH_MONITOR] alerted on {count} failed attempts (source={source}, posted={ok})")


def _reset_for_tests() -> None:
    global _last_alert
    with _lock:
        _failures.clear()
        _last_alert = None
