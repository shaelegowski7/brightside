from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
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
    # 'new' -> 'resolved' -> 'price_sanity_reject'|'matched'|'no_ean_match'|
    # 'title_mismatch'|'fetch_blocked' -> 'stage1_rejected'|'stage2_scored' ->
    # 'pinged'|'ping_failed'|'cooldown_suppressed'|'unverified_pinged'
    # (no_ean_match/title_mismatch/price_sanity_reject are terminal and
    # silent -- Fix Build Guide phase 2: don't post unverified/mismatched
    # deals, just log. unverified_pinged is the one exception, reserved for
    # pipeline.py's _UNMATCHABLE_BY_DESIGN_SOURCES, e.g. pokemon_center)
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


class StockState(Base):
    """Last-seen stock status per (retailer, url_hash) for restock/new-release
    monitors (Phase 3, e.g. Pokemon Center) — a *stock-status* transition,
    not a price comparison, hence a separate table from crawl_state (spec:
    "New-releases/stock-drop monitoring — separate module, different
    logic"). See app/sources/stock_state.py for the diffing helper."""

    __tablename__ = "stock_state"

    retailer = Column(String, primary_key=True)
    url_hash = Column(String, primary_key=True)
    in_stock = Column(Boolean, nullable=False)
    last_seen = Column(DateTime(timezone=True), default=utcnow)


class TitleSearchCache(Base):
    """Negative+positive cache for the model-number/title Keepa search
    fallback (spec priority #2) — keyed on the extracted model-number term
    (see app/matching/model_number.py), not the raw deal title, so two
    different HUKD posts mentioning the same code share one cache entry.
    asin=None means "searched, found nothing usable"; per spec, don't
    re-search the same failed term within 7 days (see
    app/matching/title_search_cache.py). A found asin has no expiry —
    matches don't go stale the way "not found yet" does."""

    __tablename__ = "title_search_cache"

    search_term = Column(String, primary_key=True)
    asin = Column(String, nullable=True)
    searched_at = Column(DateTime(timezone=True), default=utcnow)


class CategorySize(Base):
    """Cached Keepa category productCount, keyed on catId -- category sizes
    barely change, so this is a long-TTL cache (see keepa_client.
    get_category_size) that makes each distinct leaf category an
    effectively one-time Keepa cost for the velocity gate's rank-percentile
    leg (see decision/engine.py). cat_id is BigInteger, not Integer -- some
    real Keepa leaf category IDs exceed Postgres's 4-byte INTEGER range
    (confirmed live 2026-07-24: catId 30117754031, ~14x int32's max, hit
    NumericValueOutOfRange and silently dropped that deal's processing)."""

    __tablename__ = "category_size"

    cat_id = Column(BigInteger, primary_key=True)
    name = Column(String, nullable=True)
    product_count = Column(Integer, nullable=False)
    fetched_at = Column(DateTime(timezone=True), default=utcnow)


class Purchase(Base):
    """Manual purchase log (spec phase 3) -- feeds the review workflow.
    score_id ties a purchase to the exact decision-engine snapshot it was
    bought against. Users only ever see an ASIN (Discord embed links), never
    a raw score_id -- app/purchases.py resolves ASIN -> most recent Score
    server-side so callers never need to know this id."""

    __tablename__ = "purchases"

    id = Column(Integer, primary_key=True, index=True)
    score_id = Column(Integer, ForeignKey("scores.id"), nullable=False, index=True)
    qty = Column(Integer, nullable=False)
    actual_buy_price = Column(Integer, nullable=False)   # pence, per unit -- mirrors deals.buy_price
    notes = Column(String, nullable=True)
    ts = Column(DateTime(timezone=True), default=utcnow)


class Outcome(Base):
    """Manual sale outcome log (spec phase 3). purchase_id is the PK (no
    separate id column) -- matches the spec's literal schema, one outcome
    per purchase (full quantity sold together, not partial/repeat sales).
    Feeds monitoring.purchases_outcomes_summary's realised-vs-predicted ROI
    comparison -- the whole point of logging this at all (see scores.roi,
    the immutable prediction this gets checked against)."""

    __tablename__ = "outcomes"

    purchase_id = Column(Integer, ForeignKey("purchases.id"), primary_key=True)
    sold_price = Column(Integer, nullable=False)   # pence, per unit -- mirrors scores.sell_price
    sold_date = Column(DateTime(timezone=True), nullable=False)
    actual_fees = Column(Integer, nullable=True)   # pence
    notes = Column(String, nullable=True)


class FeeEstimateCache(Base):
    """SP-API getMyFeesEstimate cache (Phase 2, dormant until spapi_client.
    is_configured()) -- spec: "Cache fee estimates per (ASIN, price-band)
    for 24h." price_band_pence buckets to the nearest 100p so near-identical
    prices reuse a cache hit instead of spending an SP-API call each time."""

    __tablename__ = "spapi_fee_cache"

    asin = Column(String, primary_key=True)
    price_band_pence = Column(Integer, primary_key=True)
    referral_fee_pence = Column(Integer, nullable=False)
    fba_fulfilment_fee_pence = Column(Integer, nullable=False)
    fetched_at = Column(DateTime(timezone=True), default=utcnow)


class GatingCache(Base):
    """SP-API getListingsRestrictions cache (Phase 2, dormant until
    spapi_client.is_configured()) -- spec: "Cache gating results per ASIN
    for 7 days." """

    __tablename__ = "spapi_gating_cache"

    asin = Column(String, primary_key=True)
    gated = Column(Boolean, nullable=False)
    fetched_at = Column(DateTime(timezone=True), default=utcnow)


class TokenLog(Base):
    """One row per Keepa API call. Kept indefinitely for the first two weeks
    per the spec to validate the token-budget estimates; cheap enough to
    leave running after that."""

    __tablename__ = "token_log"

    id = Column(Integer, primary_key=True, index=True)
    ts = Column(DateTime(timezone=True), default=utcnow)
    stage = Column(String, nullable=False)   # 'stage1_screen' | 'stage2_full' | 'title_search'
    item_count = Column(Integer, nullable=False)   # ASINs/codes in the batch
    tokens_before = Column(Integer, nullable=True)
    tokens_after = Column(Integer, nullable=True)
    tokens_consumed = Column(Integer, nullable=True)   # best-effort; see keepa_client
    note = Column(String, nullable=True)
