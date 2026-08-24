"""Aggregate reporting over data the pipeline already persists -- deals.status
(a state machine, see models.py's Deal docstring) and token_log (one row per
Keepa call, see keepa_client._log_tokens). No new logging call sites needed.

Built to make the source-widening work in config.yaml (more HUKD feeds, more
Argos/Pokemon Center categories) observable: is a source producing scored
matches or just noise reaching stage1_rejected/no_ean_match/title_mismatch,
and is total Keepa spend staying inside budget now that raw deal volume is
several times higher than before that change."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models

# Mirrors dashboard.py's own _GOOD_VERDICTS -- kept as a separate local copy
# rather than a shared import, same as that module does, since each is a
# small self-contained reporting surface.
_GOOD_VERDICTS = ("PASS", "PASS_WITH_FLAGS")


def funnel_summary(db: Session, since: datetime) -> dict[str, dict[str, int]]:
    """Deals first seen since `since`, grouped by (source, current status).
    status is a single mutable column, not an event log, so this reflects
    each deal's latest state reached so far -- e.g. a deal currently
    'stage2_scored' passed through 'resolved'/'matched' too, but only its
    latest status is counted here."""
    rows = (
        db.query(models.Deal.source, models.Deal.status, func.count(models.Deal.id))
        .filter(models.Deal.first_seen >= since)
        .group_by(models.Deal.source, models.Deal.status)
        .all()
    )
    summary: dict[str, dict[str, int]] = {}
    for source, status, count in rows:
        summary.setdefault(source, {})[status] = count
    return summary


def keepa_token_summary(db: Session, since: datetime) -> dict:
    """Total Keepa tokens consumed since `since`, broken down by call stage.
    tokens_consumed can be null (see _log_tokens's wait=True refill caveat)
    -- those calls are excluded from the sum but not from existing, so this
    is a best-effort lower bound, same caveat as the underlying data."""
    rows = (
        db.query(models.TokenLog.stage, func.sum(models.TokenLog.tokens_consumed), func.count(models.TokenLog.id))
        .filter(models.TokenLog.ts >= since, models.TokenLog.tokens_consumed.isnot(None))
        .group_by(models.TokenLog.stage)
        .all()
    )
    by_stage = {stage: {"tokens": int(total or 0), "calls": calls} for stage, total, calls in rows}
    return {
        "total_consumed": sum(v["tokens"] for v in by_stage.values()),
        "by_stage": by_stage,
    }


def purchases_outcomes_summary(
    db: Session, since: datetime, until: datetime, min_roi_threshold: float | None = None
) -> dict:
    """Joins Outcome -> Purchase -> Score. Windowed on Outcome.sold_date (no
    separate "logged at" timestamp exists on outcomes -- see models.py's
    Outcome docstring) -- fine for a personal tool logged promptly.
    avg_realised_roi uses the spec's exact formula: (sold_price -
    actual_fees - actual_buy_price) / actual_buy_price. avg_predicted_roi is
    scores.roi as persisted at ping time, via each outcome's purchase. Both
    are None (not a ZeroDivisionError) when nothing's been logged yet.

    hit_rate: fraction of realised ROIs that met or exceeded
    min_roi_threshold (typically config's thresholds.min_roi -- the same
    bar a deal had to clear to get a PASS verdict in the first place).
    None whenever no threshold is supplied or no outcomes exist. A stable
    avg_realised_roi can hide a 50/50 split of big wins and losses --
    hit_rate is what actually answers "is this tool worth trusting."

    good_scores/purchase_conversion_rate: how many PASS/PASS_WITH_FLAGS
    scores landed in [since, until) vs how many purchases were logged in
    that same window -- an approximate, not exact, per-deal match (a score
    from this window might get bought later, outside it), the same
    windowing trade-off funnel_summary already makes. Answers "of the deals
    the pipeline confirmed, how many actually got bought.\""""
    rows = (
        db.query(models.Outcome, models.Purchase, models.Score)
        .join(models.Purchase, models.Outcome.purchase_id == models.Purchase.id)
        .join(models.Score, models.Purchase.score_id == models.Score.id)
        .filter(models.Outcome.sold_date >= since, models.Outcome.sold_date < until)
        .all()
    )
    realised_rois = []
    predicted_rois = []
    for outcome, purchase, score in rows:
        fees = outcome.actual_fees or 0
        if purchase.actual_buy_price:
            realised_rois.append((outcome.sold_price - fees - purchase.actual_buy_price) / purchase.actual_buy_price)
        if score.roi is not None:
            predicted_rois.append(score.roi)

    hit_rate = None
    if min_roi_threshold is not None and realised_rois:
        hits = sum(1 for r in realised_rois if r >= min_roi_threshold)
        hit_rate = hits / len(realised_rois)

    purchases_logged = (
        db.query(func.count(models.Purchase.id))
        .filter(models.Purchase.ts >= since, models.Purchase.ts < until)
        .scalar() or 0
    )

    good_scores = (
        db.query(func.count(models.Score.id))
        .filter(models.Score.ts >= since, models.Score.ts < until, models.Score.verdict.in_(_GOOD_VERDICTS))
        .scalar() or 0
    )
    purchase_conversion_rate = (purchases_logged / good_scores) if good_scores else None

    return {
        "outcomes_recorded": len(rows),
        "purchases_logged": purchases_logged,
        "avg_realised_roi": (sum(realised_rois) / len(realised_rois)) if realised_rois else None,
        "avg_predicted_roi": (sum(predicted_rois) / len(predicted_rois)) if predicted_rois else None,
        "hit_rate": hit_rate,
        "good_scores": good_scores,
        "purchase_conversion_rate": purchase_conversion_rate,
    }


def ping_latency_summary(db: Session, since: datetime) -> dict:
    """Elapsed time between a deal first appearing (Deal.first_seen) and it
    being pinged (Ping.ts). Retail arbitrage deals go stale fast -- stock
    sells out, clearance prices get corrected -- so a creeping latency here
    means confirmed deals are dying before anyone can act on them, with
    nothing else in this app that would surface it. Computed in Python, not
    SQL, to stay portable across the sqlite test DB and Postgres prod (see
    conftest.py) -- same approach purchases_outcomes_summary already uses
    for its ROI averaging."""
    rows = (
        db.query(models.Ping.ts, models.Deal.first_seen)
        .join(models.Deal, models.Ping.deal_id == models.Deal.id)
        .filter(models.Ping.ts >= since)
        .all()
    )
    latencies_minutes = [
        (ping_ts - first_seen).total_seconds() / 60
        for ping_ts, first_seen in rows
        if ping_ts is not None and first_seen is not None
    ]
    return {
        "pings_measured": len(latencies_minutes),
        "avg_latency_minutes": (sum(latencies_minutes) / len(latencies_minutes)) if latencies_minutes else None,
        "max_latency_minutes": max(latencies_minutes) if latencies_minutes else None,
    }


def build_weekly_summary(db: Session, hours: int = 168, min_roi_threshold: float | None = None) -> dict:
    until = datetime.now(timezone.utc)
    since = until - timedelta(hours=hours)
    pings = (
        db.query(func.count(models.Ping.id))
        .filter(models.Ping.ts >= since, models.Ping.ts < until)
        .scalar() or 0
    )
    return {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "hours": hours,
        "pings": pings,
        **purchases_outcomes_summary(db, since, until, min_roi_threshold=min_roi_threshold),
    }


def build_summary(db: Session, hours: int = 24) -> dict:
    until = datetime.now(timezone.utc)
    since = until - timedelta(hours=hours)
    return {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "hours": hours,
        "by_source": funnel_summary(db, since),
        "keepa_tokens": keepa_token_summary(db, since),
        "ping_latency": ping_latency_summary(db, since),
    }
