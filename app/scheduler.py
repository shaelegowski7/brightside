"""APScheduler wiring for the hotukdeals poll loop — mirrors sentimentfx-
backend's BackgroundScheduler + CronTrigger/IntervalTrigger pattern: one
job function, its own DB session, per-item try/except so one bad deal
doesn't abort the batch, max_instances=1 + coalesce=True so a slow poll
doesn't pile up overlapping runs."""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from . import pipeline
from .config import get_config, get_settings
from .database import SessionLocal
from .decision.engine import DecisionConfig
from .pricing.fees import FeeTableProvider
from .sources.argos import ArgosAdapter
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


def poll_argos_clearance() -> None:
    app_cfg = get_config()
    argos_cfg = app_cfg.get("argos", {})
    decision_cfg = DecisionConfig.from_app_config(app_cfg)
    fee_provider = FeeTableProvider(app_cfg["fees"])
    adapter = ArgosAdapter(
        category_urls=argos_cfg["category_urls"],
        api_key=get_settings().scraperapi_key,
        min_delay_s=argos_cfg["min_delay_seconds"],
        max_delay_s=argos_cfg["max_delay_seconds"],
    )

    db = SessionLocal()
    try:
        raw_deals = adapter.crawl(db)
        print(f"[SCHEDULER] argos: {len(raw_deals)} new/changed item(s)")
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

    argos_cfg = app_cfg.get("argos", {})
    if argos_cfg.get("enabled", False):
        argos_interval = argos_cfg["poll_interval_minutes"]
        scheduler.add_job(
            poll_argos_clearance,
            IntervalTrigger(minutes=argos_interval),
            id="poll_argos_clearance",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        print(f"[SCHEDULER] argos clearance enabled, polling every {argos_interval}m")

    scheduler.start()
    print(f"[SCHEDULER] started, polling every {interval_minutes}m")
