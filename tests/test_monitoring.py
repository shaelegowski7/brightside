from datetime import datetime, timedelta, timezone

from app import models, monitoring


def _deal(source: str, status: str, url: str, first_seen: datetime) -> models.Deal:
    return models.Deal(
        source=source, title="t", url=url, buy_price=1000,
        status=status, first_seen=first_seen, last_seen=first_seen,
    )


def test_funnel_summary_groups_by_source_and_status(db_session):
    now = datetime.now(timezone.utc)
    db_session.add_all([
        _deal("hotukdeals", "pinged", "https://x/1", now),
        _deal("hotukdeals", "no_ean_match", "https://x/2", now),
        _deal("hotukdeals", "no_ean_match", "https://x/3", now),
        _deal("argos", "stage2_scored", "https://x/4", now),
    ])
    db_session.commit()

    summary = monitoring.funnel_summary(db_session, since=now - timedelta(hours=1))

    assert summary["hotukdeals"]["pinged"] == 1
    assert summary["hotukdeals"]["no_ean_match"] == 2
    assert summary["argos"]["stage2_scored"] == 1


def test_funnel_summary_excludes_deals_outside_window(db_session):
    now = datetime.now(timezone.utc)
    db_session.add(_deal("hotukdeals", "pinged", "https://x/old", now - timedelta(days=2)))
    db_session.commit()

    summary = monitoring.funnel_summary(db_session, since=now - timedelta(hours=1))

    assert summary == {}


def test_keepa_token_summary_sums_by_stage(db_session):
    now = datetime.now(timezone.utc)
    db_session.add_all([
        models.TokenLog(ts=now, stage="stage1_screen", item_count=10, tokens_consumed=15),
        models.TokenLog(ts=now, stage="stage1_screen", item_count=5, tokens_consumed=8),
        models.TokenLog(ts=now, stage="stage2_full", item_count=2, tokens_consumed=26),
        models.TokenLog(ts=now - timedelta(days=2), stage="stage1_screen", item_count=1, tokens_consumed=100),
    ])
    db_session.commit()

    result = monitoring.keepa_token_summary(db_session, since=now - timedelta(hours=1))

    assert result["by_stage"]["stage1_screen"] == {"tokens": 23, "calls": 2}
    assert result["by_stage"]["stage2_full"] == {"tokens": 26, "calls": 1}
    assert result["total_consumed"] == 49


def test_keepa_token_summary_excludes_null_consumed(db_session):
    now = datetime.now(timezone.utc)
    db_session.add(models.TokenLog(ts=now, stage="stage1_screen", item_count=1, tokens_consumed=None))
    db_session.commit()

    result = monitoring.keepa_token_summary(db_session, since=now - timedelta(hours=1))

    assert result["total_consumed"] == 0
    assert result["by_stage"] == {}


def test_build_summary_shape(db_session):
    summary = monitoring.build_summary(db_session, hours=12)
    assert summary["hours"] == 12
    assert "by_source" in summary
    assert "keepa_tokens" in summary
    assert summary["keepa_tokens"]["total_consumed"] == 0
