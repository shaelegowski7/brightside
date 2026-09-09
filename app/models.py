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
    (see app/matching/model_number.py) where one exists, so two different
    HUKD posts mentioning the same code share one cache entry. For
    pipeline.py's _FULL_TITLE_SEARCH_SOURCES (GTIN-less single-product
    scrapers with no model-number token to key on), it's keyed on the raw
    deal title instead — still fine as a cache key since it's a real
    retailer product title, not a search query someone's likely to retype.
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
    for 7 days."

    `gated` alone is not enough to make a buying decision. SP-API's
    reasonCode distinguishes APPROVAL_REQUIRED (there is an application
    path -- send a qualifying trade invoice and you are in) from
    NOT_ELIGIBLE (no path exists at all). For wholesale sourcing those are
    opposite answers: the first is a paperwork step you can plan an order
    around, the second kills the SKU. `reason_code` and `approval_url`
    keep that distinction; `gated` stays for existing callers.
    """

    __tablename__ = "spapi_gating_cache"

    asin = Column(String, primary_key=True)
    gated = Column(Boolean, nullable=False)
    # "APPROVAL_REQUIRED" | "NOT_ELIGIBLE" | "ASIN_NOT_FOUND" | None when ungated.
    # Multiple codes on one ASIN are stored comma-separated, sorted.
    reason_code = Column(String, nullable=True)
    # Seller Central deep link Amazon returns with an APPROVAL_REQUIRED
    # restriction -- the actual "apply here" URL, worth keeping so a gated
    # candidate is one click from an application.
    approval_url = Column(String, nullable=True)
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


class CandidateAsin(Base):
    """Products found by candidate_finder.py that already clear every
    structural gate in decision/engine.py -- persisted purely so a
    scheduled run reports only genuinely new finds instead of re-posting
    the same shopping list every day. NOT deals: no buy price is known
    for these, nobody has sourced them, and they never enter the pipeline
    (see candidate_finder.py's module docstring). Prices are refreshed on
    every sighting since Amazon's buybox moves the target with it."""

    __tablename__ = "candidate_asins"

    asin = Column(String, primary_key=True)
    title = Column(String, nullable=True)
    buybox_price = Column(Integer, nullable=False)        # pence
    target_buy_price = Column(Integer, nullable=False)    # pence -- source at/below this
    sales_rank = Column(Integer, nullable=True)
    fba_offer_count = Column(Integer, nullable=True)
    est_monthly_sales = Column(Float, nullable=True)
    first_seen = Column(DateTime(timezone=True), default=utcnow)
    last_seen = Column(DateTime(timezone=True), default=utcnow)


class NdaToysCrawlProgress(Base):
    """Single-row resume cursor for NdaToysAdapter -- persists the index
    into config.yaml's nda_toys.category_urls list of the last brand page
    whose full listing (every page of it) has been walked to completion.
    Lets a stopped/restarted crawl skip brands already fully covered
    instead of re-walking every listing page (each an expensive
    ultra_premium fetch) from the start every time -- see
    app/sources/nda_toys.py. Committed after each category_url finishes,
    not just at the end, so a mid-run interruption only loses progress on
    the one brand in flight, same reasoning as crawl_state's deferred
    recording."""

    __tablename__ = "nda_toys_crawl_progress"

    id = Column(Integer, primary_key=True)
    completed_through_index = Column(Integer, nullable=False, default=-1)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class EbayListing(Base):
    """One row per SKU we have tried to list on eBay -- the record of what
    the lister actually did, so a re-run is resumable and a partial batch
    is diagnosable. eBay's bulk endpoints report per-item failures in-band
    with an HTTP 200 (see ebay_client.py), so "the call succeeded" is never
    enough on its own; `status` is the per-item verdict.

    Deliberately keyed on our own `sku`, not eBay's ids: the SKU is the one
    identifier that exists before any eBay call is made, which is what lets
    a crashed run pick up where it left off."""

    __tablename__ = "ebay_listings"

    sku = Column(String, primary_key=True)
    title = Column(String, nullable=False)
    isbn = Column(String, nullable=True, index=True)
    condition = Column(String, nullable=False)      # eBay ConditionEnum, e.g. USED_GOOD
    price_pence = Column(Integer, nullable=False)
    quantity = Column(Integer, nullable=False, default=1)
    category_id = Column(String, nullable=True)
    # 'pending' -> 'inventory_created' -> 'offer_created' -> 'published',
    # or 'failed' at whichever stage stopped it (last_error says which).
    status = Column(String, nullable=False, default="pending", index=True)
    offer_id = Column(String, nullable=True, index=True)
    listing_id = Column(String, nullable=True, index=True)
    last_error = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class SupplierScanResult(Base):
    """Per-product outcome of a catalogue-first scan (tools/wholesale_scan.py).

    Until 2026-09-09 these scans wrote nothing to the database: 15,416
    stage-1 lookups and 2,578 full scorings across six suppliers lived only
    in loose {supplier}_stage*_checkpoint.jsonl files in the repo root,
    untracked by git and one `git clean` from gone. That made the obvious
    cross-supplier questions unanswerable without a one-off script each
    time -- "everything that cleared £3 net", "did this supplier's re-quote
    beat their last one" -- despite the Keepa tokens for all of it having
    already been spent.

    One row per (supplier, ean). The checkpoints stay as they are: they are
    the resume mechanism and are written mid-batch, whereas this is the
    queryable record written once a product has an outcome.

    A product that never reached stage 2 is still stored, with stage=1 and
    a null verdict -- "matched an ASIN but was screened out cheaply" and
    "was never seen" are different facts and both worth keeping. Prices are
    overwritten on re-scan since a supplier's quote and Amazon's buy box
    both move; first_seen/last_seen bound when the observation held.
    """

    __tablename__ = "supplier_scan_results"

    supplier = Column(String, primary_key=True)
    ean = Column(String, primary_key=True)

    asin = Column(String, nullable=True, index=True)
    brand = Column(String, nullable=True)
    name = Column(String, nullable=True)          # supplier's own description
    title = Column(String, nullable=True)         # Amazon's title

    stage = Column(Integer, nullable=False)       # 1 = screened out / no match, 2 = fully scored
    buy_price_pence = Column(Integer, nullable=True)
    units_per_sale = Column(Integer, nullable=True)
    bundle_cost_pence = Column(Integer, nullable=True)
    sell_price_pence = Column(Integer, nullable=True)
    net_profit_pence = Column(Integer, nullable=True)
    roi = Column(Float, nullable=True)
    sales_rank = Column(Integer, nullable=True)
    fba_offer_count = Column(Integer, nullable=True)
    # Velocity is the biggest single reject reason across every scan so far.
    # Source is kept alongside the number because they mean different things:
    # "keepa_confirmed" is Keepa's real monthlySold badge (bucketed, lowest
    # bucket 50); "rank_drop_proxy" is a noisy stand-in from 30-day rank drops.
    est_monthly_sales = Column(Float, nullable=True)
    est_monthly_sales_source = Column(String, nullable=True)

    verdict = Column(String, nullable=True)       # PASS / PASS_WITH_FLAGS / REJECT
    verdict_reason = Column(String, nullable=True)
    flags = Column(JSON, nullable=True)
    note = Column(String, nullable=True)

    first_seen = Column(DateTime(timezone=True), default=utcnow)
    last_seen = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
