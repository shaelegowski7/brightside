"""APScheduler wiring for the hotukdeals poll loop — mirrors sentimentfx-
backend's BackgroundScheduler + CronTrigger/IntervalTrigger pattern: one
job function, its own DB session, per-item try/except so one bad deal
doesn't abort the batch, max_instances=1 + coalesce=True so a slow poll
doesn't pile up overlapping runs."""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from . import pipeline
from .config import get_config
from .database import SessionLocal
from .decision.engine import DecisionConfig
from .pricing.fees import FeeTableProvider
from .sources.hotukdeals import HotUKDealsAdapter

scheduler = BackgroundScheduler()


def poll_hukd_feeds() -> None:
    app_cfg = get_config()
    decision_cfg = DecisionConfig.from_app_config(app_cfg)
    fee_provider = FeeTableProvider(app_cfg["fees"])

    db = SessionLocal()
    try:
        for feed in app_cfg["hukd"]["feeds"]:
            adapter = HotUKDealsAdapter(feed["url"])
            try:
                raw_deals = adapter.poll()
            except Exception as e:
                print(f"[SCHEDULER] {feed['name']}: poll failed: {e}")
                continue
            print(f"[SCHEDULER] {feed['name']}: {len(raw_deals)} item(s)")
            for raw in raw_deals:
                try:
                    pipeline.process_deal(db, raw, decision_cfg, fee_provider, app_cfg)
                except Exception as e:
                    db.rollback()
                    print(f"[SCHEDULER] {raw.url}: processing error: {e}")
    finally:
        db.close()


def start_scheduler() -> None:
    app_cfg = get_config()
    interval_minutes = app_cfg["hukd"]["poll_interval_minutes"]
    scheduler.add_job(
        poll_hukd_feeds,
        IntervalTrigger(minutes=interval_minutes),
        id="poll_hukd_feeds",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.start()
    print(f"[SCHEDULER] started, polling every {interval_minutes}m")
