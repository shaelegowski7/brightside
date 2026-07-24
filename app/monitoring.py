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


def build_summary(db: Session, hours: int = 24) -> dict:
    until = datetime.now(timezone.utc)
    since = until - timedelta(hours=hours)
    return {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "hours": hours,
        "by_source": funnel_summary(db, since),
        "keepa_tokens": keepa_token_summary(db, since),
    }
