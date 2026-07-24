"""APScheduler wiring for the hotukdeals poll loop — mirrors sentimentfx-
backend's BackgroundScheduler + CronTrigger/IntervalTrigger pattern: one
job function, its own DB session, per-item try/except so one bad deal
doesn't abort the batch, max_instances=1 + coalesce=True so a slow poll
doesn't pile up overlapping runs."""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from . import discord_notifier, monitoring, pipeline
from .config import get_config, get_settings
from .database import SessionLocal
from .decision.engine import DecisionConfig
from .pricing import fees
from .sources.argos import ArgosAdapter
from .sources.hotukdeals import HotUKDealsAdapter
from .sources.pokemon_center import PokemonCenterAdapter
from .sources.smyths import SmythsAdapter

scheduler = BackgroundScheduler()


def poll_hukd_feeds() -> None:
    app_cfg = get_config()
    decision_cfg = DecisionConfig.from_app_config(app_cfg)
    merchant_blocklist = {m.lower() for m in app_cfg["hukd"].get("merchant_blocklist", [])}

    db = SessionLocal()
    try:
        fee_provider = fees.build_fee_provider(db, app_cfg)
        for feed in app_cfg["hukd"]["feeds"]:
            adapter = HotUKDealsAdapter(feed["url"])
            try:
                raw_deals = adapter.poll()
            except Exception as e:
                print(f"[SCHEDULER] {feed['name']}: poll failed: {e}")
                continue
            print(f"[SCHEDULER] {feed['name']}: {len(raw_deals)} item(s)")
            for raw in raw_deals:
                if raw.retailer and raw.retailer.lower() in merchant_blocklist:
                    continue
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
    adapter = ArgosAdapter(
        category_urls=argos_cfg["category_urls"],
        api_key=get_settings().scraperapi_key,
        min_delay_s=argos_cfg["min_delay_seconds"],
        max_delay_s=argos_cfg["max_delay_seconds"],
    )

    db = SessionLocal()
    fee_provider = fees.build_fee_provider(db, app_cfg)

    def _on_deal(raw) -> None:
        try:
            pipeline.process_deal(db, raw, decision_cfg, fee_provider, app_cfg)
        except Exception as e:
            db.rollback()
            print(f"[SCHEDULER] {raw.url}: processing error: {e}")

    try:
        count = adapter.crawl(db, on_deal=_on_deal)
        print(f"[SCHEDULER] argos: {count} new/changed item(s)")
    finally:
        db.close()


def poll_smyths_clearance() -> None:
    app_cfg = get_config()
    smyths_cfg = app_cfg.get("smyths", {})
    decision_cfg = DecisionConfig.from_app_config(app_cfg)
    adapter = SmythsAdapter(
        category_urls=smyths_cfg["category_urls"],
        api_key=get_settings().scraperapi_key,
        min_delay_s=smyths_cfg["min_delay_seconds"],
        max_delay_s=smyths_cfg["max_delay_seconds"],
    )

    db = SessionLocal()
    fee_provider = fees.build_fee_provider(db, app_cfg)

    def _on_deal(raw) -> None:
        try:
            pipeline.process_deal(db, raw, decision_cfg, fee_provider, app_cfg)
        except Exception as e:
            db.rollback()
            print(f"[SCHEDULER] {raw.url}: processing error: {e}")

    try:
        count = adapter.crawl(db, on_deal=_on_deal)
        print(f"[SCHEDULER] smyths: {count} new/changed item(s)")
    finally:
        db.close()


def poll_pokemon_center() -> None:
    app_cfg = get_config()
    pc_cfg = app_cfg.get("pokemon_center", {})
    decision_cfg = DecisionConfig.from_app_config(app_cfg)
    adapter = PokemonCenterAdapter(
        category_urls=pc_cfg["category_urls"],
        api_key=get_settings().scraperapi_key,
        min_delay_s=pc_cfg["min_delay_seconds"],
        max_delay_s=pc_cfg["max_delay_seconds"],
    )

    db = SessionLocal()
    fee_provider = fees.build_fee_provider(db, app_cfg)

    def _on_deal(raw) -> None:
        try:
            pipeline.process_deal(db, raw, decision_cfg, fee_provider, app_cfg)
        except Exception as e:
            db.rollback()
            print(f"[SCHEDULER] {raw.url}: processing error: {e}")

    try:
        count = adapter.crawl(db, on_deal=_on_deal)
        print(f"[SCHEDULER] pokemon_center: {count} drop(s)")
    finally:
        db.close()


def post_daily_summary() -> None:
    app_cfg = get_config()
    monitoring_cfg = app_cfg.get("monitoring", {})
    hours = monitoring_cfg.get("summary_window_hours", 24)
    token_budget = monitoring_cfg.get("daily_token_budget_alert")

    db = SessionLocal()
    try:
        summary = monitoring.build_summary(db, hours=hours)
    finally:
        db.close()

    embed = discord_notifier.build_summary_embed(summary, token_budget_alert=token_budget)
    ok = discord_notifier.send_ping(get_settings().discord_webhook_url, embed)
    print(f"[SCHEDULER] daily summary posted: {ok}")


def post_weekly_summary() -> None:
    app_cfg = get_config()
    monitoring_cfg = app_cfg.get("monitoring", {})
    hours = monitoring_cfg.get("weekly_summary_window_hours", 168)

    db = SessionLocal()
    try:
        summary = monitoring.build_weekly_summary(db, hours=hours)
    finally:
        db.close()

    embed = discord_notifier.build_weekly_summary_embed(summary)
    ok = discord_notifier.send_ping(get_settings().discord_webhook_url, embed)
    print(f"[SCHEDULER] weekly summary posted: {ok}")


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

    smyths_cfg = app_cfg.get("smyths", {})
    if smyths_cfg.get("enabled", False):
        smyths_interval = smyths_cfg["poll_interval_minutes"]
        scheduler.add_job(
            poll_smyths_clearance,
            IntervalTrigger(minutes=smyths_interval),
            id="poll_smyths_clearance",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        print(f"[SCHEDULER] smyths clearance enabled, polling every {smyths_interval}m")

    pc_cfg = app_cfg.get("pokemon_center", {})
    if pc_cfg.get("enabled", False):
        pc_interval = pc_cfg["poll_interval_minutes"]
        scheduler.add_job(
            poll_pokemon_center,
            IntervalTrigger(minutes=pc_interval),
            id="poll_pokemon_center",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        print(f"[SCHEDULER] pokemon_center enabled, polling every {pc_interval}m")

    monitoring_cfg = app_cfg.get("monitoring", {})
    if monitoring_cfg.get("daily_summary_enabled", True):
        scheduler.add_job(
            post_daily_summary,
            IntervalTrigger(hours=24),
            id="post_daily_summary",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        print("[SCHEDULER] daily summary enabled, posting every 24h")

    if monitoring_cfg.get("weekly_summary_enabled", True):
        scheduler.add_job(
            post_weekly_summary,
            IntervalTrigger(hours=168),
            id="post_weekly_summary",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        print("[SCHEDULER] weekly summary enabled, posting every 168h")

    scheduler.start()
    print(f"[SCHEDULER] started, polling every {interval_minutes}m")
