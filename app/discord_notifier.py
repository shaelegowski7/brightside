"""Discord webhook notifier + ASIN-keyed cooldown.

Spec: "pings (id, asin, deal_id, score_id, ts) — cooldown keyed on ASIN, not
deal: same product via two sources must not double-ping." Dedup rule: don't
re-ping the same ASIN within cooldown_hours unless buy price improved by at
least cooldown_price_improve_pct vs the deal on the most recent ping.
"""
from datetime import datetime, timedelta, timezone

import requests
from sqlalchemy.orm import Session

from . import models
from .decision.engine import ScoreResult, Verdict

_TIMEOUT_SECONDS = 10
COLOR_GREEN = 0x2ECC71
COLOR_AMBER = 0xF1C40F


def should_ping(db: Session, asin: str, buy_price_pence: int, cooldown_hours: int, price_improve_pct: float) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=cooldown_hours)
    last_ping = (
        db.query(models.Ping)
        .filter(models.Ping.asin == asin, models.Ping.ts >= cutoff)
        .order_by(models.Ping.ts.desc())
        .first()
    )
    if last_ping is None:
        return True
    last_deal = db.get(models.Deal, last_ping.deal_id)
    if last_deal is None or not last_deal.buy_price:
        return True
    improvement = (last_deal.buy_price - buy_price_pence) / last_deal.buy_price
    return improvement >= price_improve_pct


def record_ping(db: Session, asin: str, deal_id: int, score_id: int) -> models.Ping:
    ping = models.Ping(asin=asin, deal_id=deal_id, score_id=score_id)
    db.add(ping)
    db.commit()
    db.refresh(ping)
    return ping


def _money(pence: int | None) -> str:
    return f"£{pence / 100:.2f}" if pence is not None else "—"


def _field(name: str, value: str, inline: bool = True) -> dict:
    return {"name": name, "value": value, "inline": inline}


def build_matched_embed(
    *,
    title: str,
    retailer_url: str,
    image_url: str | None,
    retailer: str | None,
    asin: str,
    buy_price_pence: int,
    result: ScoreResult,
    est_monthly_sales: float | None,
    offer_count: int | None,
    amazon_on_listing: bool | None,
    gated: bool | None,
    match_confidence: str,
) -> dict:
    color = COLOR_GREEN if result.verdict == Verdict.PASS else COLOR_AMBER
    gating_str = "Gated" if gated is True else ("Clear" if gated is False else "Not checked (no SP-API yet)")
    keepa_url = f"https://keepa.com/#!product/2-{asin}"
    amazon_url = f"https://www.amazon.co.uk/dp/{asin}"

    fields = [
        _field("Buy price", _money(buy_price_pence)),
        _field("Est. sell price", _money(result.sell_price_pence)),
        _field("Net profit", _money(result.net_profit_pence)),
        _field("ROI", f"{result.roi:.0%}" if result.roi is not None else "—"),
        _field("Est. monthly sales", str(int(est_monthly_sales)) if est_monthly_sales else "—"),
        _field("FBA offers", str(offer_count) if offer_count is not None else "—"),
        _field("Amazon on listing?", "Yes" if amazon_on_listing else "No"),
        _field("Gating", gating_str),
        _field("Match confidence", match_confidence),
        _field("Links", f"[Keepa chart]({keepa_url}) · [Amazon UK]({amazon_url})", inline=False),
    ]
    embed = {"title": title, "url": retailer_url, "color": color, "fields": fields}
    if image_url:
        embed["thumbnail"] = {"url": image_url}
    footer_text = f"{retailer} · flags: {', '.join(result.flags)}" if retailer and result.flags else (
        retailer or (f"flags: {', '.join(result.flags)}" if result.flags else None)
    )
    if footer_text:
        embed["footer"] = {"text": footer_text}
    return embed


def build_unverified_embed(
    *, title: str, retailer_url: str, image_url: str | None, retailer: str | None, buy_price_pence: int
) -> dict:
    """Only used for pipeline.py's _UNMATCHABLE_BY_DESIGN_SOURCES (currently
    just pokemon_center, which has no EAN/matching mechanism at all) --
    ordinary HUKD/retailer deals that fail to match are dropped silently
    instead (Fix Build Guide phase 2)."""
    embed = {
        "title": f"UNVERIFIED MATCH — check manually: {title}",
        "url": retailer_url,
        "color": COLOR_AMBER,
        "fields": [
            _field("Buy price", _money(buy_price_pence)),
            _field("Retailer", retailer or "—"),
        ],
        "footer": {"text": "No Amazon match found automatically — verify by hand before buying"},
    }
    if image_url:
        embed["thumbnail"] = {"url": image_url}
    return embed


def build_summary_embed(summary: dict, token_budget_alert: int | None = None) -> dict:
    """Daily funnel + Keepa token-spend digest -- see monitoring.build_summary
    for the data shape. `token_budget_alert` (config.yaml monitoring.
    daily_token_budget_alert) flips the embed amber when exceeded so an
    over-budget day is visible without reading numbers closely."""
    by_source = summary["by_source"]
    tokens = summary["keepa_tokens"]

    lines = []
    for source in sorted(by_source):
        counts = by_source[source]
        total = sum(counts.values())
        scored = counts.get("stage2_scored", 0)
        pinged = counts.get("pinged", 0) + counts.get("unverified_pinged", 0)
        lines.append(f"**{source}**: {total} seen · {scored} scored · {pinged} pinged")
    source_text = "\n".join(lines) if lines else "No deals seen in this window."

    total_tokens = tokens["total_consumed"]
    over_budget = token_budget_alert is not None and total_tokens > token_budget_alert
    token_str = (
        f"{total_tokens} (budget {token_budget_alert})" if token_budget_alert is not None else str(total_tokens)
    )

    embed = {
        "title": f"Daily summary — last {summary['hours']}h",
        "color": COLOR_AMBER if over_budget else COLOR_GREEN,
        "fields": [
            _field("Sources", source_text, inline=False),
            _field("Keepa tokens used", token_str, inline=False),
        ],
    }
    return embed


def send_ping(webhook_url: str, embed: dict) -> bool:
    try:
        resp = requests.post(webhook_url, json={"embeds": [embed]}, timeout=_TIMEOUT_SECONDS)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"[DISCORD] webhook post failed: {e}")
        return False
