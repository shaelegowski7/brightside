from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
)

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Product(Base):
    """EAN/ASIN match cache. Checked before any Keepa call — permanent, and
    negative results (ean set, asin/confidence show no Amazon match) are
    cached too so we never re-spend tokens on a known dead end."""

    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    ean = Column(String, unique=True, nullable=True, index=True)
    asin = Column(String, unique=True, nullable=True, index=True)
    title = Column(String, nullable=True)
    matched_via = Column(String, nullable=False)   # 'amazon_url' | 'jsonld' | 'jsonld_no_match'
    confidence = Column(String, nullable=False)     # 'high' | 'none'
    created_at = Column(DateTime(timezone=True), default=utcnow)


class Deal(Base):
    """One row per hotukdeals thread (or future source item). `url` is the
    source's own deal link — the natural dedupe key for the poll loop.
    `retailer_url` is filled in once the HUKD redirect has been resolved."""

    __tablename__ = "deals"

    id = Column(Integer, primary_key=True, index=True)
    source = Column(String, nullable=False, index=True)     # 'hotukdeals'
    retailer = Column(String, nullable=True)                # merchant name from feed
    title = Column(String, nullable=False)
    image_url = Column(String, nullable=True)
    url = Column(String, unique=True, nullable=False, index=True)
    retailer_url = Column(String, nullable=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=True, index=True)
    buy_price = Column(Integer, nullable=False)   # pence
    first_seen = Column(DateTime(timezone=True), default=utcnow)
    last_seen = Column(DateTime(timezone=True), default=utcnow)
    # 'new' -> 'resolved' -> 'matched'|'no_ean_match'|'fetch_blocked' ->
    # 'stage1_rejected'|'stage2_scored' -> 'pinged'|'cooldown_suppressed'|'unverified_pinged'
    status = Column(String, nullable=False, default="new", index=True)


class Score(Base):
    """Immutable decision-engine snapshot for one deal at one point in time.
    Only created for deals that reached financial evaluation (had an ASIN) —
    see verdict/verdict_reason for the outcome and flags_json for soft flags."""

    __tablename__ = "scores"

    id = Column(Integer, primary_key=True, index=True)
    deal_id = Column(Integer, ForeignKey("deals.id"), nullable=False, index=True)
    ts = Column(DateTime(timezone=True), default=utcnow)
    sell_price = Column(Integer, nullable=True)   # pence; null if no_sell_price reject
    fees_json = Column(JSON, nullable=True)
    net_profit = Column(Integer, nullable=True)   # pence
    roi = Column(Float, nullable=True)
    rank = Column(Integer, nullable=True)
    est_monthly_sales = Column(Float, nullable=True)
    offer_count = Column(Integer, nullable=True)
    amazon_on_listing = Column(Boolean, nullable=True)
    gated = Column(Boolean, nullable=True)   # null: not checked (no SP-API yet)
    flags_json = Column(JSON, nullable=True)   # list[str] soft flags
    verdict = Column(String, nullable=False)   # 'PASS' | 'PASS_WITH_FLAGS' | 'REJECT'
    verdict_reason = Column(String, nullable=True)


class Ping(Base):
    """Cooldown ledger, keyed on ASIN (not deal_id) — the same product
    surfacing via two different sources/deals must not double-ping."""

    __tablename__ = "pings"

    id = Column(Integer, primary_key=True, index=True)
    asin = Column(String, nullable=False, index=True)
    deal_id = Column(Integer, ForeignKey("deals.id"), nullable=False)
    score_id = Column(Integer, ForeignKey("scores.id"), nullable=False)
    ts = Column(DateTime(timezone=True), default=utcnow)


class CrawlState(Base):
    """Last-seen price per (retailer, url_hash) for retailer clearance
    scrapers (Phase 2). Diffing against this is how a scraper knows to emit
    an event only for a new item or a price drop vs the previous crawl —
    an unchanged row means "skip, nothing to do". url_hash is a sha256 hex
    digest of the product URL (see app/sources/crawl_state.py), not the raw
    URL, so the key stays a fixed, indexable size regardless of URL length."""

    __tablename__ = "crawl_state"

    retailer = Column(String, primary_key=True)
    url_hash = Column(String, primary_key=True)
    last_price = Column(Integer, nullable=False)   # pence
    last_seen = Column(DateTime(timezone=True), default=utcnow)


class TokenLog(Base):
    """One row per Keepa API call. Kept indefinitely for the first two weeks
    per the spec to validate the token-budget estimates; cheap enough to
    leave running after that."""

    __tablename__ = "token_log"

    id = Column(Integer, primary_key=True, index=True)
    ts = Column(DateTime(timezone=True), default=utcnow)
    stage = Column(String, nullable=False)   # 'stage1_screen' | 'stage2_full'
    item_count = Column(Integer, nullable=False)   # ASINs/codes in the batch
    tokens_before = Column(Integer, nullable=True)
    tokens_after = Column(Integer, nullable=True)
    tokens_consumed = Column(Integer, nullable=True)   # best-effort; see keepa_client
    note = Column(String, nullable=True)
