"""Manual on-demand crawl trigger for the PWA's "run crawl now" button --
reuses the exact same poll_* functions app/scheduler.py's own interval
triggers call, just invoked directly and with their progress tracked in
memory so the frontend can poll live status instead of guessing. Runs in a
background thread because a full pass across every source can take several
minutes (Argos/Smyths/B&Q/Screwfix/Home Bargains/John Lewis each have their
own per-page fetch delays plus real Keepa calls) -- the triggering HTTP
request must return immediately, not block for that long.

Single-process assumption: state lives in a module-level global, fine for
this app's one Railway instance (see Procfile), not safe to run >1 replica
of this service without moving it to the DB or a shared cache.

Deliberately does NOT coordinate with APScheduler's own scheduled runs of
these same poll_* functions -- only guards against two manual triggers
overlapping. A manual run colliding with a scheduled one for the same
source is a rare edge case for a 3-user tool (worst case: duplicate
Keepa/HTTP calls in that window, not data corruption, since crawl_state/
_upsert_deal are individually committed) and not worth a cross-thread lock
shared with the scheduler for this."""
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import scheduler
from .config import get_config

# (state key, display label, scheduler function name, config key -- None
# means always enabled, e.g. hukd has no on/off switch of its own).
_SOURCES: list[tuple[str, str, str, str | None]] = [
    ("hukd", "HotUKDeals", "poll_hukd_feeds", None),
    ("argos", "Argos", "poll_argos_clearance", "argos"),
    ("smyths", "Smyths Toys", "poll_smyths_clearance", "smyths"),
    ("bq", "B&Q", "poll_bq_clearance", "bq"),
    ("screwfix", "Screwfix", "poll_screwfix_clearance", "screwfix"),
    ("homebargains", "Home Bargains", "poll_homebargains_clearance", "homebargains"),
    ("johnlewis", "John Lewis", "poll_johnlewis_outlet", "johnlewis"),
    ("pokemon_center", "Pokemon Center", "poll_pokemon_center", "pokemon_center"),
]


@dataclass
class SourceRun:
    key: str
    label: str
    status: str = "pending"   # pending | running | done | error | skipped
    new_deals: int | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "key": self.key, "label": self.label, "status": self.status,
            "new_deals": self.new_deals, "error": self.error,
        }


@dataclass
class CrawlRun:
    running: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
    sources: list[SourceRun] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "running": self.running,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "sources": [s.to_dict() for s in self.sources],
        }


_state_lock = threading.Lock()
_current = CrawlRun()


def get_status() -> dict:
    with _state_lock:
        return _current.to_dict()


def start_crawl() -> tuple[bool, dict]:
    """Returns (started, status). started=False means a run is already in
    progress -- the caller gets that run's current status back rather than
    a second one being kicked off."""
    global _current
    with _state_lock:
        if _current.running:
            return False, _current.to_dict()
        app_cfg = get_config()
        run = CrawlRun(
            running=True,
            started_at=datetime.now(timezone.utc),
            sources=[
                SourceRun(key=key, label=label)
                if cfg_key is None or app_cfg.get(cfg_key, {}).get("enabled", False)
                else SourceRun(key=key, label=label, status="skipped")
                for key, label, _fn_name, cfg_key in _SOURCES
            ],
        )
        _current = run
    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return True, run.to_dict()


def _run() -> None:
    for key, _label, fn_name, _cfg_key in _SOURCES:
        with _state_lock:
            source = next(s for s in _current.sources if s.key == key)
            if source.status == "skipped":
                continue
            source.status = "running"

        try:
            count = getattr(scheduler, fn_name)()
        except Exception as e:
            print(f"[CRAWL_RUNNER] {key}: failed: {e}")
            with _state_lock:
                source.status = "error"
                source.error = str(e)
            continue

        with _state_lock:
            source.status = "done"
            source.new_deals = count

    with _state_lock:
        _current.running = False
        _current.finished_at = datetime.now(timezone.utc)
