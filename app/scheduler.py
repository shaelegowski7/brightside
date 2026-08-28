"""APScheduler wiring for the hotukdeals poll loop — mirrors sentimentfx-
backend's BackgroundScheduler + CronTrigger/IntervalTrigger pattern: one
job function, its own DB session, per-item try/except so one bad deal
doesn't abort the batch, max_instances=1 + coalesce=True so a slow poll
doesn't pile up overlapping runs."""
from dataclasses import dataclass

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from . import discord_notifier, monitoring, pipeline
from .config import get_config, get_settings
from .database import SessionLocal
from .decision.engine import DecisionConfig
from .pricing import fees
from .sources.argos import ArgosAdapter
from .sources.bq import BQAdapter
from .sources.homebargains import HomeBargainsAdapter
from .sources.hotukdeals import HotUKDealsAdapter
from .sources.johnlewis import JohnLewisAdapter
from .sources.nda_toys import NdaToysAdapter
from .sources.pokemon_center import PokemonCenterAdapter
from .sources.screwfix import ScrewfixAdapter
from .sources.smyths import SmythsAdapter

scheduler = BackgroundScheduler()


def poll_hukd_feeds() -> int:
    app_cfg = get_config()
    decision_cfg = DecisionConfig.from_app_config(app_cfg)
    merchant_blocklist = {m.lower() for m in app_cfg["hukd"].get("merchant_blocklist", [])}

    processed_count = 0
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
                    processed_count += 1
                except Exception as e:
                    db.rollback()
                    print(f"[SCHEDULER] {raw.url}: processing error: {e}")
    finally:
        db.close()
    return processed_count


@dataclass(frozen=True)
class _ClearanceSource:
    """One entry per retailer clearance scraper. `key` indexes both
    config.yaml (category_urls/delays/enabled) and the source's own log
    lines. `needs_api_key` is True for the sources whose adapter proxies
    through ScraperAPI (see each adapter's own __init__ in app/sources/).
    Adding a new clearance source is then a one-line addition to
    _CLEARANCE_SOURCES + a thin poll_* wrapper below, instead of a whole new
    copy-pasted poll function and start_scheduler() block -- this mirrors
    crawl_runner.py's own _SOURCES table instead of duplicating it
    independently, which is how the two drifted apart before."""

    key: str
    adapter_cls: type
    needs_api_key: bool


_CLEARANCE_SOURCES: dict[str, _ClearanceSource] = {
    "argos": _ClearanceSource("argos", ArgosAdapter, needs_api_key=True),
    "smyths": _ClearanceSource("smyths", SmythsAdapter, needs_api_key=True),
    "bq": _ClearanceSource("bq", BQAdapter, needs_api_key=False),
    "screwfix": _ClearanceSource("screwfix", ScrewfixAdapter, needs_api_key=False),
    "homebargains": _ClearanceSource("homebargains", HomeBargainsAdapter, needs_api_key=False),
    "johnlewis": _ClearanceSource("johnlewis", JohnLewisAdapter, needs_api_key=False),
    "pokemon_center": _ClearanceSource("pokemon_center", PokemonCenterAdapter, needs_api_key=True),
    # Deliberately absent from start_scheduler()'s clearance_jobs below --
    # manual-trigger-only (crawl_runner.py's _SOURCES), not on a timer. Every
    # fetch here uses ScraperAPI's ultra_premium tier (NDA Toys' bot
    # protection rejects the standard pool -- see sources/nda_toys.py),
    # which costs meaningfully more per request than every other source; an
    # unattended recurring job would burn credits without anyone watching.
    "nda_toys": _ClearanceSource("nda_toys", NdaToysAdapter, needs_api_key=True),
}


def _run_clearance_poll(source: _ClearanceSource) -> int:
    """Shared body for every poll_*_clearance/poll_pokemon_center function
    below: build the adapter, open one DB session for the whole crawl, feed
    each found item through the pipeline with its own try/except (so one bad
    deal can't abort the crawl), and always close the session -- including
    if adapter construction or build_fee_provider() itself raises, which the
    previous per-source copies didn't guard (db was opened before their
    try/finally)."""
    app_cfg = get_config()
    src_cfg = app_cfg.get(source.key, {})
    decision_cfg = DecisionConfig.from_app_config(app_cfg)

    kwargs = dict(
        category_urls=src_cfg["category_urls"],
        min_delay_s=src_cfg["min_delay_seconds"],
        max_delay_s=src_cfg["max_delay_seconds"],
    )
    if source.needs_api_key:
        kwargs["api_key"] = get_settings().scraperapi_key

    db = SessionLocal()
    try:
        adapter = source.adapter_cls(**kwargs)
        fee_provider = fees.build_fee_provider(db, app_cfg)

        def _on_deal(raw) -> None:
            try:
                pipeline.process_deal(db, raw, decision_cfg, fee_provider, app_cfg)
            except Exception as e:
                db.rollback()
                print(f"[SCHEDULER] {raw.url}: processing error: {e}")

        count = adapter.crawl(db, on_deal=_on_deal)
        print(f"[SCHEDULER] {source.key}: {count} new/changed item(s)")
        return count
    finally:
        db.close()


# Thin named wrappers -- kept as real top-level functions (not dynamically
# generated) because crawl_runner._SOURCES and the tests monkeypatch these
# by exact name (getattr(scheduler, fn_name); monkeypatch.setattr(scheduler,
# "poll_argos_clearance", ...)), and start_scheduler() below registers each
# as an APScheduler job under id=<function name>.
def poll_argos_clearance() -> int:
    return _run_clearance_poll(_CLEARANCE_SOURCES["argos"])


def poll_smyths_clearance() -> int:
    return _run_clearance_poll(_CLEARANCE_SOURCES["smyths"])


def poll_bq_clearance() -> int:
    return _run_clearance_poll(_CLEARANCE_SOURCES["bq"])


def poll_screwfix_clearance() -> int:
    return _run_clearance_poll(_CLEARANCE_SOURCES["screwfix"])


def poll_homebargains_clearance() -> int:
    return _run_clearance_poll(_CLEARANCE_SOURCES["homebargains"])


def poll_johnlewis_outlet() -> int:
    return _run_clearance_poll(_CLEARANCE_SOURCES["johnlewis"])


def poll_pokemon_center() -> int:
    return _run_clearance_poll(_CLEARANCE_SOURCES["pokemon_center"])


def poll_nda_toys_deals() -> int:
    """Manual-trigger-only -- see _CLEARANCE_SOURCES' nda_toys entry.
    Named/kept as a real top-level function for the same reason as every
    other poll_* here: crawl_runner._SOURCES and tests call it by exact
    name via getattr(scheduler, "poll_nda_toys_deals")."""
    return _run_clearance_poll(_CLEARANCE_SOURCES["nda_toys"])


def post_daily_summary() -> None:
    app_cfg = get_config()
    monitoring_cfg = app_cfg.get("monitoring", {})
    hours = monitoring_cfg.get("summary_window_hours", 24)
    token_budget = monitoring_cfg.get("daily_token_budget_alert")
    blocked_rate_alert_pct = monitoring_cfg.get("blocked_rate_alert_pct")

    db = SessionLocal()
    try:
        summary = monitoring.build_summary(db, hours=hours)
    finally:
        db.close()

    embed = discord_notifier.build_summary_embed(
        summary, token_budget_alert=token_budget, blocked_rate_alert_pct=blocked_rate_alert_pct
    )
    ok = discord_notifier.send_ping(get_settings().discord_webhook_url, embed)
    print(f"[SCHEDULER] daily summary posted: {ok}")


def post_weekly_summary() -> None:
    app_cfg = get_config()
    monitoring_cfg = app_cfg.get("monitoring", {})
    hours = monitoring_cfg.get("weekly_summary_window_hours", 168)
    # .get(), not [...] -- unlike the scoring path, a missing/incomplete
    # thresholds block here should degrade to "no hit rate this week", not
    # crash the whole summary job.
    min_roi = (app_cfg.get("thresholds") or {}).get("min_roi")

    db = SessionLocal()
    try:
        summary = monitoring.build_weekly_summary(db, hours=hours, min_roi_threshold=min_roi)
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

    # (config key, poll function) -- one entry per _CLEARANCE_SOURCES source,
    # registered identically instead of seven copy-pasted enable/add_job
    # blocks. Job id matches the function's own name, same convention as
    # before (crawl_runner.py's manual-trigger path calls these same
    # functions by name via getattr(scheduler, fn_name)).
    clearance_jobs = [
        ("argos", poll_argos_clearance),
        ("smyths", poll_smyths_clearance),
        ("bq", poll_bq_clearance),
        ("screwfix", poll_screwfix_clearance),
        ("homebargains", poll_homebargains_clearance),
        ("johnlewis", poll_johnlewis_outlet),
        ("pokemon_center", poll_pokemon_center),
    ]
    for cfg_key, poll_fn in clearance_jobs:
        src_cfg = app_cfg.get(cfg_key, {})
        if not src_cfg.get("enabled", False):
            continue
        interval = src_cfg["poll_interval_minutes"]
        scheduler.add_job(
            poll_fn,
            IntervalTrigger(minutes=interval),
            id=poll_fn.__name__,
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        print(f"[SCHEDULER] {cfg_key} enabled, polling every {interval}m")

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
