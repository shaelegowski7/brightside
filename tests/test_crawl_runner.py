"""In-memory state machine behind the PWA's manual "run crawl now" button --
real network/DB work is entirely in the poll_* functions it delegates to
(already exercised elsewhere), so these tests stub those out and focus on
the orchestration: enabled/disabled sources, error isolation, and refusing
a second concurrent run."""
import threading
import time

from app import crawl_runner, scheduler

_ALL_DISABLED = {
    "argos": {"enabled": False}, "smyths": {"enabled": False}, "bq": {"enabled": False},
    "screwfix": {"enabled": False}, "homebargains": {"enabled": False},
    "johnlewis": {"enabled": False}, "pokemon_center": {"enabled": False},
}


def _wait_until_done(timeout: float = 2.0) -> None:
    start = time.time()
    while crawl_runner.get_status()["running"]:
        if time.time() - start > timeout:
            raise TimeoutError("crawl did not finish in time")
        time.sleep(0.01)


def test_start_crawl_runs_enabled_sources_and_skips_disabled(monkeypatch):
    monkeypatch.setattr(crawl_runner, "get_config", lambda: {
        **_ALL_DISABLED, "argos": {"enabled": True}, "bq": {"enabled": True},
    })
    monkeypatch.setattr(scheduler, "poll_hukd_feeds", lambda: 3)
    monkeypatch.setattr(scheduler, "poll_argos_clearance", lambda: 5)
    monkeypatch.setattr(scheduler, "poll_bq_clearance", lambda: 0)

    started, _ = crawl_runner.start_crawl()
    assert started is True
    _wait_until_done()

    final = crawl_runner.get_status()
    by_key = {s["key"]: s for s in final["sources"]}
    assert by_key["hukd"]["status"] == "done" and by_key["hukd"]["new_deals"] == 3
    assert by_key["argos"]["status"] == "done" and by_key["argos"]["new_deals"] == 5
    assert by_key["bq"]["status"] == "done" and by_key["bq"]["new_deals"] == 0
    assert by_key["smyths"]["status"] == "skipped"
    assert by_key["smyths"]["new_deals"] is None
    assert final["running"] is False
    assert final["started_at"] is not None
    assert final["finished_at"] is not None


def test_start_crawl_captures_error_without_stopping_other_sources(monkeypatch):
    monkeypatch.setattr(crawl_runner, "get_config", lambda: {**_ALL_DISABLED, "argos": {"enabled": True}})
    monkeypatch.setattr(scheduler, "poll_hukd_feeds", lambda: 1)

    def _boom():
        raise RuntimeError("argos exploded")
    monkeypatch.setattr(scheduler, "poll_argos_clearance", _boom)

    crawl_runner.start_crawl()
    _wait_until_done()

    final = crawl_runner.get_status()
    by_key = {s["key"]: s for s in final["sources"]}
    assert by_key["hukd"]["status"] == "done" and by_key["hukd"]["new_deals"] == 1
    assert by_key["argos"]["status"] == "error"
    assert "argos exploded" in by_key["argos"]["error"]


def test_start_crawl_with_sources_only_runs_the_selected_ones(monkeypatch):
    """sources= restricts a run to just those keys -- e.g. resuming NDA
    Toys alone without re-running every fast source ahead of it in
    _SOURCES. Unselected sources shouldn't appear in the run at all,
    not even as "skipped" (that status means config-disabled)."""
    monkeypatch.setattr(crawl_runner, "get_config", lambda: {
        **_ALL_DISABLED, "argos": {"enabled": True}, "bq": {"enabled": True},
    })
    calls = []
    monkeypatch.setattr(scheduler, "poll_hukd_feeds", lambda: calls.append("hukd") or 1)
    monkeypatch.setattr(scheduler, "poll_argos_clearance", lambda: calls.append("argos") or 2)
    monkeypatch.setattr(scheduler, "poll_bq_clearance", lambda: calls.append("bq") or 3)

    started, _ = crawl_runner.start_crawl(sources=["argos"])
    assert started is True
    _wait_until_done()

    assert calls == ["argos"]
    final = crawl_runner.get_status()
    assert [s["key"] for s in final["sources"]] == ["argos"]


def test_start_crawl_refuses_second_run_while_in_progress(monkeypatch):
    monkeypatch.setattr(crawl_runner, "get_config", lambda: _ALL_DISABLED)
    release = threading.Event()

    def _blocking_hukd():
        release.wait(timeout=2.0)
        return 1
    monkeypatch.setattr(scheduler, "poll_hukd_feeds", _blocking_hukd)

    started1, _ = crawl_runner.start_crawl()
    assert started1 is True

    started2, status2 = crawl_runner.start_crawl()
    assert started2 is False
    assert status2["running"] is True

    release.set()
    _wait_until_done()
