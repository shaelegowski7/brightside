# FBA Deal Scanner — Project Spec

Private online/retail arbitrage sourcing tool for 3 users. Watches UK deal sources and retailer clearance sections, scores items against Amazon UK sell-side data (Keepa + SP-API), and pings profitable finds to a private Discord. Also supports in-store barcode scanning via a mobile PWA.

**Not a SaaS. No auth beyond a shared secret. Optimise for build speed and low running cost.**

---

## Stack (match existing skills/infra)

- **Backend:** Python 3.12, FastAPI, deployed on Railway
- **DB:** PostgreSQL (Railway or Supabase)
- **Scheduler:** APScheduler inside the FastAPI app (or Railway cron)
- **Frontend (scanner PWA):** single-page app, plain JS or React/Vite on Vercel. Uses `html5-qrcode` or native `BarcodeDetector` API
- **Alerts:** Discord webhook (embeds)
- **External APIs:** Keepa API (base tier, ~20 tokens/min), Amazon SP-API (Selling Partner API)

---

## Data sources

### 1. Hotukdeals (RSS — primary online source)
- Poll RSS feeds every 5–10 min: "hottest", plus keyword/category feeds (config-driven list)
- Each item: title, deal URL, price (parse from title), merchant, timestamp
- Follow the deal link through HUKD's redirect wrapper to resolve the **final retailer URL**

### 2. Retailer clearance sections (scrapers, one module per retailer)
- Phase 1: Argos clearance. Later: Currys clearance outlet, Smyths, B&M-style sites where feasible
- Scrape politely: respect crawl-delay, randomised 5–15s between requests, identifiable UA, cache pages, only diff against previous crawl
- Emit events only for: **new item** or **price drop** vs last crawl
- On first run per retailer: full pass of current clearance stock (one-off backfill of *current* inventory only — no historical backfill ever)

### 3. In-store barcode scans (PWA)
- PWA scans EAN → POST `/scan {ean, buy_price}` → runs the same scoring pipeline synchronously → returns verdict to the UI **and** posts to Discord

---

## EAN → ASIN matching (critical path)

Priority order:
1. **Structured data extraction** from the resolved retailer page: JSON-LD / microdata `gtin13`/`gtin`/`ean` fields. Most large UK retailers embed this (Google Shopping requirement). Build a per-retailer extractor with a generic JSON-LD fallback.
2. **Model number regex** from deal title (patterns like `AF300UK`, alphanumeric model codes) → Keepa product search by code/title. Note: Keepa search/finder requests cost more tokens than a plain product lookup (~10), so only use this fallback after path 1 fails, and cache negative results too (don't re-search the same failed title within 7 days).
3. If neither: mark match as `confidence=low` and still ping, flagged **"UNVERIFIED MATCH — check manually"**. Never silently drop; never present low-confidence numbers as trustworthy.

Cache every EAN→ASIN mapping in Postgres permanently. Check cache before any Keepa call.

---

## Keepa usage — two-stage lookup (token budget matters)

Token economics: base request ≈ 1 token (includes full price/rank **history** + `stats` free). `offers` parameter ≈ 6 tokens per page of 10 offers (buy box holder + stock counts require `offers`). Tokens expire 60 min after generation → max bank ≈ 1,200 on base tier.

**Stage 1 — cheap screen (1–2 tokens):** basic product request with `stats`. Use cached buy-box/price history to estimate sell price. Kill the deal if:
- estimated ROI below threshold on optimistic assumptions, or
- sales rank worse than category threshold, or
- category in blocklist

**Stage 2 — full lookup (~13 tokens, `offers=20`):** only for stage-1 survivors. Gets live offers, FBA/FBM breakdown, buy box holder, stock. 

Use the official `keepa` Python library with `wait=True` (auto-queues on token exhaustion). Batch ASINs (up to 100/request) for crawl passes.

Expected spend: ~500–1,300 tokens/day baseline; ~2,000–4,500 on store-run days. Base tier ≈ 28,800/day — plenty.

---

## Amazon SP-API

Requires a Pro seller account registered as a developer (LWA auth). Two endpoints:
- **`getMyFeesEstimate`** — exact referral + FBA fulfilment fee for ASIN at intended sell price
- **`getListingsRestrictions`** — gating check for *our* seller account

Cache fee estimates per (ASIN, price-band) for 24h. Cache gating results per ASIN for 7 days.

If SP-API isn't set up yet (approval pending), fall back to a category-based fee estimate table (config file: referral % per category + FBA fee by size tier) and mark pings `fees=estimated`.

---

## Decision engine

Inputs: buy_price (from deal/scan), sell_price (buy box; if Amazon retail holds buy box, see filters), fees, dimensions/weight.

```
fee_vat_mult   = 1.0 if vat_registered else 1.20   # Amazon charges 20% VAT on fees; unrecoverable if not VAT-registered
total_fees     = (referral_fee + fba_fulfilment_fee) * fee_vat_mult

our_share      = est_monthly_sales / (fba_offer_count + 1)   # naive equal-share assumption
est_months_to_sell = clamp(1 / max(our_share, 0.1), 1, 6)    # min 1 month, cap at 6

net_profit = sell_price
           - total_fees
           - (monthly_storage_fee * est_months_to_sell)
           - inbound_shipping_per_unit   # config, default £0.40
roi = net_profit / buy_price
```

Sell price source: current buy box. If buy box is suppressed/absent, fall back to lowest live FBA offer and add soft flag `no_buybox`. If no live FBA offers at all, hard-reject with reason `no_sell_price`.

Keep the calc explicit and commented — this will be tuned.

**Hard filters (auto-reject, return reason):**
- `roi < 0.30` or `net_profit < £3` (config)
- Amazon retail is on the listing / holds buy box
- FBA offer count > 6 (config)
- Sales rank worse than per-category threshold (config table)
- Gated for our account (if SP-API live)
- Hazmat/meltable flag
- Oversize tier (config toggle)

**Soft flags (ping but annotate):** low match confidence, estimated fees, buy box price >20% above 90-day average (spike risk), <90 days of rank history.

---

## Discord output

Webhook embed per passing deal:
- Title (linked to retailer page), thumbnail if available
- Buy price · est. sell price · **net profit** · **ROI %**
- Est. monthly sales (Keepa rank-drop estimate) · FBA offer count · Amazon-on-listing? 
- Gating status · match confidence
- Link: Keepa chart (`https://keepa.com/#!product/2-{ASIN}`), Amazon UK listing
- Colour-code: green = all clear; amber = soft flags present

Dedupe: don't re-ping same ASIN within 24h unless price improved ≥10%. Cooldown table in Postgres.

In-store scans: always return verdict to the PWA (pass **or** fail with reason list); only passes go to Discord.

---

## Database schema (minimal)

```sql
products      (id PK, ean UNIQUE NULLABLE, asin, title, matched_via, confidence, created_at)
              -- ean nullable: model-number matches have an ASIN but no EAN
deals         (id, source, retailer, url, product_id FK, buy_price, first_seen, last_seen, status)
scores        (id, deal_id, ts, sell_price, fees_json, net_profit, roi,
               rank, est_monthly_sales, offer_count, amazon_on_listing,
               gated, flags_json, verdict, verdict_reason)   -- decision snapshot, immutable
pings         (id, asin, deal_id, score_id, ts)              -- cooldown keyed on ASIN, not deal:
              -- same product via two sources must not double-ping
crawl_state   (retailer, url_hash, last_price, last_seen, PK(retailer, url_hash))
purchases     (id, score_id, qty, actual_buy_price, notes, ts) -- manual log, feeds review
outcomes      (purchase_id, sold_price, sold_date, actual_fees, notes) -- manual, phase 3
```

The `scores` snapshot is the feedback loop: what we believed at decision time vs what happened.

---

## Config (single YAML/env-driven)

- ROI/profit thresholds, offer-count max, per-category rank thresholds
- Category blocklist, oversize toggle, `vat_registered`
- HUKD feed list, retailer scraper toggles + schedules
- Discord webhook URL, shared secret for PWA endpoint
- Keepa/SP-API credentials

---

## Build phases

**Phase 1 (MVP):** HUKD RSS → link resolve → JSON-LD EAN extract → Keepa two-stage → decision engine with fee-table fallback → Discord ping → cooldown. Postgres for cache + scores.

**Phase 2:** SP-API exact fees + gating. Argos clearance scraper with diff detection + day-one current-stock pass. PWA barcode scanner hitting `/scan`.

**Phase 3:** purchases/outcomes logging + a weekly Discord summary (hit rate, realised vs predicted ROI). Additional retailer scrapers. New-releases/stock-drop monitoring (separate module — different logic, don't bolt into clearance pipeline).

---

## Non-goals

- No user accounts, billing, or public access
- No historical price archiving (Keepa returns history on every request — persist decision snapshots only)
- No auto-purchasing. Humans buy; the tool filters.

## Notes for implementation

- Keepa library: `pip install keepa`; UK domain. `stats` param is free; only add `offers` in stage 2.
- HUKD/retailer scraping is for personal use — keep rates polite, handle Cloudflare gracefully (skip + log rather than fight it).
- All timestamps UTC. All money in pence internally (int), format at display.
- Log every Keepa call with token cost to a `token_log` table for the first two weeks to validate the budget estimates above.
