"""Shared engine for scanning a wholesaler's price list against the same
stage1 -> stage2 -> score_deal pipeline every scraper source runs (see
app/pipeline.py). Extracted 2026-09-02 from scan_rashmian_feed.py and
scan_price_list.py, which had converged on near-identical logic -- with
wholesale-catalog scanning now the primary sourcing workflow (not a one-off),
duplicating this per supplier stopped making sense.

Not run directly. Each wholesaler gets a thin loader script that parses its
own file format into a list of FeedRow (the one thing that's genuinely
different per supplier: column names, VAT handling, availability filters)
and calls run_scan(). See scan_rashmian_feed.py and scan_price_list.py for
the pattern.

CHECKPOINTED (confirmed necessary live 2026-09-02: a multi-hour, multi-
thousand-item scan against a shared, rate-limited Keepa token budget WILL
get interrupted by something eventually, and losing every lookup already
paid for is not acceptable at this scale). Both stages append their
per-item results to <source_name>_stage{1,2}_checkpoint.jsonl as they go;
a rerun loads whatever's there and only queries Keepa for what's missing.
Delete the checkpoint files for a genuinely fresh run.

Gating is computed and reported per-PASS item but NOT used to filter --
gating status is worth knowing, not worth discarding a real financial match
over (see app/candidate_finder.py's same convention). Locally this is moot
anyway since SP-API creds live on Railway only.
"""
import json
import os
import re
import socket
import sys
import time
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path


def _fix_railway_internal_db_url() -> None:
    """Run BEFORE app.database is imported (it builds its engine at import
    time from whatever DATABASE_URL says).

    These scans want Railway's SP-API credentials, and the way to get them
    locally is `railway run -- python tools/scan_x_feed.py`. But that also
    injects Railway's *internal* DATABASE_URL (postgres.railway.internal),
    which only resolves inside Railway's own network -- locally every scan
    dies instantly with "could not translate host name". The local .env has
    the public proxy URL for the same database.

    So: if DATABASE_URL points at an unresolvable .railway.internal host and
    .env offers a different one, use .env's. Checked by actually resolving
    the name rather than pattern-matching alone, so this stays inert when
    the code really is running inside Railway.
    """
    url = os.environ.get("DATABASE_URL", "")
    if ".railway.internal" not in url:
        return
    host = url.split("@")[-1].split("/")[0].split(":")[0]
    try:
        socket.getaddrinfo(host, None)
        return                      # resolves -- we're inside Railway, leave it
    except socket.gaierror:
        pass
    try:
        from dotenv import dotenv_values
        local = dotenv_values(Path(__file__).resolve().parent.parent / ".env").get("DATABASE_URL")
    except Exception:
        local = None
    if local and local != url:
        os.environ["DATABASE_URL"] = local
        print(f"[SCAN] {host} is unreachable from here -- using .env's DATABASE_URL instead")

_fix_railway_internal_db_url()   # MUST precede app.database's import-time engine build

from app import keepa_client, scan_store, spapi_client
from app.config import get_config
from app.database import SessionLocal
from app.decision.engine import DecisionConfig, ScoreInput, Verdict, score_deal
from app.keepa_client import (
    KEEPA_DOMAIN,
    _IDX_BUY_BOX_SHIPPING,
    _IDX_NEW,
    _IDX_SALES_RANK,
    _category_name,
    _csv_value,
    _get_client,
    _log_tokens,
    _rank_history_days,
    Stage1Result,
)
from app.pricing.fees import SizeDims, build_fee_provider

_REPO_ROOT = Path(__file__).resolve().parent.parent
CHUNK = 100
_GATING_DELAY_S = 0.3


@dataclass
class FeedRow:
    ean: str
    brand: str
    name: str
    buy_price_pence: int   # the real cash cost per unit -- inc VAT wherever
                            # the supplier's terms mean VAT is genuinely
                            # payable and non-reclaimable. Compute this in
                            # the loader, not here -- VAT treatment varies
                            # per supplier (flat rate, per-row tax code, or
                            # none) and the loader is the only place that
                            # knows the source file's convention.
    list_price: float      # supplier's own quoted price, for the report
    note: str = ""          # free-text context for the report, e.g. "T1 VAT"
                            # or "x10 multiple" -- whatever's worth a human
                            # seeing next to the price that this dataclass
                            # doesn't have a dedicated field for.


# Real incident, 2026-09-05 (pharmazon scan): 9/9 "PASS" results turned out
# to be a single unit's EAN matched to an ASIN that was actually a multipack
# bundle of that same unit ("Valupak Vitamin D3 1000Iu Tablet" at 0.83/unit
# matched to Keepa's "6 X Valupak Vitamin D 1000Iu") -- some FBA seller
# shrink-wrapped N singles and reused the single unit's barcode rather than
# registering a proper multipack GTIN, so Amazon's catalog (and therefore
# Keepa) has the wrong EAN attached to a "Pack of N"/"Case of N" ASIN. Every
# one of those 9 was actually a loss once corrected for the real N-unit cost
# needed to fulfil what the listing promises.
#
# This can't be fixed by trusting Keepa's title alone -- some source rows are
# *themselves* genuinely a multi-unit product (e.g. a wholesaler selling
# "12-Pack" of pads as its own retail unit), so a bare number in the ASIN
# title isn't inherently wrong. What's wrong is when the ASIN's implied
# multiplier is bigger than what the feed row's own name already says --
# that gap is the number of extra units you'd actually need to buy that the
# ROI math didn't account for. Reported as a flag, not auto-filtered or
# auto-corrected (same convention as the gating check below): a regex over
# free-text titles will misfire sometimes, and silently rescaling buy_price
# on a guessed multiplier risks being confidently wrong in the other
# direction. A human should look at the specific pair of titles before
# trusting either number.
_PACK_PATTERNS = [
    re.compile(r"packs?\s+of\s+(\d+)"),
    re.compile(r"case\s+of\s+(\d+)"),
    re.compile(r"(\d+)\s*x\b"),
    re.compile(r"\bx\s*(\d+)\b"),
    re.compile(r"(\d+)\s*[- ]packs?\b"),
]


def _pack_multiplier(text: str | None) -> int | None:
    """How many sellable units a title implies, multiplying each distinct
    nesting level found (e.g. "Case of 8 Packs of 12" -> 8 * 12 = 96).
    Text with no multipack language returns 1.

    Returns **None** when there is no text to read, which is not the same
    answer as 1 and must not be collapsed into it. This previously returned
    1 for a missing title, so an absent Keepa title compared equal to a
    feed's implied 1 and the pack-size check silently passed. That is how
    every Pharmazon candidate reached the report unflagged while the real
    ASINs were "Euthymol ... Pack of 5", "6 x Deep Heat Heat Rub 100g",
    "Cymex Cream for Cold Sores x 6" and so on (verified against the live
    listings, 2026-09-06) -- priced as singles, they showed 300-550% ROI;
    priced as the 5- and 6-packs they actually are, four of five lose
    money. Unknown has to stay visibly unknown.
    """
    if not text:
        return None
    t = text.lower()
    found = []
    for pat in _PACK_PATTERNS:
        for m in pat.finditer(t):
            try:
                n = int(m.group(1))
            except (IndexError, ValueError):
                continue
            if 1 < n <= 1000:
                found.append(n)
    if not found:
        return 1
    total = 1
    for n in sorted(set(found), reverse=True):
        total *= n
    return total


def _bundle_units(feed_mult: int | None, asin_mult: int | None) -> int:
    """How many feed units go into one sale on this ASIN.

    Buying N singles and shipping them as a set is ordinary FBA bundling,
    so a bigger ASIN pack is a cost multiplier, not a reject. Only whole
    multiples count: a 3-unit feed item against a 2-pack ASIN is not
    something you can assemble, so it stays at 1 and the caller flags it
    for a human. Unknown on either side also stays at 1 -- scoring at face
    value and saying so beats inventing a multiplier.
    """
    if feed_mult is None or asin_mult is None:
        return 1
    if asin_mult > feed_mult and asin_mult % feed_mult == 0:
        return asin_mult // feed_mult
    return 1


def _stage1_by_ean(db, eans_batch: list[str], source_name: str) -> dict[str, tuple[str, Stage1Result]]:
    """Same query keepa_client.stage1_screen makes, but also returns eanList
    so results can be mapped back to the specific input row -- stage1_screen
    only keys by ASIN, useless here where we start from the EAN."""
    client = _get_client()
    tokens_before = client.tokens_left
    products = client.query(
        eans_batch, domain=KEEPA_DOMAIN, stats=90, offers=None,
        product_code_is_asin=False, wait=True,
    )
    _log_tokens(db, f"{source_name}_stage1", len(eans_batch), tokens_before, client.tokens_left)

    wanted = set(eans_batch)
    matched: dict[str, tuple[str, Stage1Result]] = {}
    for product in products:
        asin = product.get("asin")
        if not asin:
            continue
        hit_eans = [e for e in (product.get("eanList") or []) if e in wanted]
        if not hit_eans:
            continue
        stats = product.get("stats") or {}
        avg90 = stats.get("avg90")
        current = stats.get("current")
        est_sell = _csv_value(avg90, _IDX_BUY_BOX_SHIPPING) or _csv_value(avg90, _IDX_NEW)
        s1 = Stage1Result(
            asin=asin,
            title=product.get("title"),
            category=_category_name(product),
            sales_rank=_csv_value(current, _IDX_SALES_RANK),
            est_sell_price_pence=int(est_sell) if est_sell is not None else None,
            rank_history_days=_rank_history_days(product),
        )
        for e in hit_eans:
            matched[e] = (asin, s1)
    return matched


def _load_checkpoint(path: Path, key: str) -> dict[str, dict]:
    if not path.exists():
        return {}
    checkpoint: dict[str, dict] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            checkpoint[entry[key]] = entry
    return checkpoint


def run_scan(source_name: str, rows: list[FeedRow]) -> None:
    """source_name becomes the prefix for every output/checkpoint file
    (<source_name>_candidates.txt, <source_name>_stage{1,2}_checkpoint.jsonl)
    and the Keepa token-log stage label -- keep it short and stable across
    reruns of the same supplier's list so checkpoints keep matching up.

    Crashes log their own traceback before propagating -- see _run_scan.
    These scans run for hours under a restart wrapper whose stderr nobody
    captures, so an unhandled exception showed up only as "scan died
    (exit 1)" with no way to tell what broke (novanex, 2026-09-06, twice).
    Printing to stdout puts it in the same log as the progress lines.
    """
    try:
        _run_scan(source_name, rows)
    except BaseException as e:   # noqa: BLE001 -- re-raised immediately
        print(f"[SCAN:{source_name}] DIED: {type(e).__name__}: {e}")
        traceback.print_exc(file=sys.stdout)
        sys.stdout.flush()
        raise


def _run_scan(source_name: str, rows: list[FeedRow]) -> None:
    out_path = _REPO_ROOT / f"{source_name}_candidates.txt"
    stage1_checkpoint_path = _REPO_ROOT / f"{source_name}_stage1_checkpoint.jsonl"
    stage2_checkpoint_path = _REPO_ROOT / f"{source_name}_stage2_checkpoint.jsonl"

    by_ean = {r.ean: r for r in rows}
    print(f"[SCAN:{source_name}] {len(rows)} usable rows")

    db = SessionLocal()
    app_cfg = get_config()
    cfg = DecisionConfig.from_app_config(app_cfg)
    fee_provider = build_fee_provider(db, app_cfg)
    check_gating = spapi_client.is_configured()
    print(f"[SCAN:{source_name}] gating check {'enabled' if check_gating else 'disabled (no local SP-API creds)'}")

    # --- stage 1, resumable ---
    stage1_checkpoint = _load_checkpoint(stage1_checkpoint_path, "ean")
    if stage1_checkpoint:
        print(f"[SCAN:{source_name}] resuming: {len(stage1_checkpoint)} EANs already checkpointed from stage1")

    stage1_matched = 0
    stage1_pass = 0
    stage1_survivors: dict[str, str] = {}   # asin -> ean

    # Replay checkpointed entries first so counts/survivors reflect the
    # whole run (this session's + prior sessions'), not just new work.
    for ean, entry in stage1_checkpoint.items():
        row = by_ean.get(ean)
        if row is None or entry.get("asin") is None:
            continue
        stage1_matched += 1
        s1 = Stage1Result(
            asin=entry["asin"], title=entry.get("title"), category=entry.get("category"),
            sales_rank=entry.get("sales_rank"), est_sell_price_pence=entry.get("est_sell_price_pence"),
            rank_history_days=entry.get("rank_history_days"),
        )
        passes, _ = keepa_client.stage1_screen_passes(s1, row.buy_price_pence, cfg, fee_provider)
        if passes:
            stage1_pass += 1
            stage1_survivors[entry["asin"]] = ean

    eans = [e for e in by_ean.keys() if e not in stage1_checkpoint]
    print(f"[SCAN:{source_name}] {len(eans)} EANs left to query for stage1 "
          f"({len(by_ean) - len(eans)} already checkpointed)")

    with open(stage1_checkpoint_path, "a", encoding="utf-8") as ckpt:
        for i in range(0, len(eans), CHUNK):
            batch = eans[i:i + CHUNK]
            matched = _stage1_by_ean(db, batch, source_name)
            stage1_matched += len(matched)
            for ean in batch:
                hit = matched.get(ean)
                if hit is None:
                    ckpt.write(json.dumps({"ean": ean, "asin": None}) + "\n")
                    continue
                asin, s1 = hit
                ckpt.write(json.dumps({"ean": ean, **asdict(s1)}) + "\n")
                row = by_ean[ean]
                passes, reason = keepa_client.stage1_screen_passes(s1, row.buy_price_pence, cfg, fee_provider)
                if passes:
                    stage1_pass += 1
                    stage1_survivors[asin] = ean
            ckpt.flush()
            done = min(i + CHUNK, len(eans))
            print(f"[SCAN:{source_name}] stage1 {done}/{len(eans)} new -- {stage1_matched} matched on Keepa total, "
                  f"{stage1_pass} survive the optimistic screen total")

    print(f"[SCAN:{source_name}] stage1 done: {stage1_matched}/{len(by_ean)} matched a Keepa product, "
          f"{stage1_pass} survive the cheap screen -- running stage2 on those")

    # --- stage 2, resumable ---
    stage2_checkpoint = _load_checkpoint(stage2_checkpoint_path, "asin")
    if stage2_checkpoint:
        print(f"[SCAN:{source_name}] resuming: {len(stage2_checkpoint)} ASINs already checkpointed from stage2")

    results: list[dict] = list(stage2_checkpoint.values())
    survivor_asins = [a for a in stage1_survivors.keys() if a not in stage2_checkpoint]
    print(f"[SCAN:{source_name}] {len(survivor_asins)} ASINs left to query for stage2 "
          f"({len(stage1_survivors) - len(survivor_asins)} already checkpointed)")

    with open(stage2_checkpoint_path, "a", encoding="utf-8") as ckpt:
        for i in range(0, len(survivor_asins), CHUNK):
            batch = survivor_asins[i:i + CHUNK]
            stage2_by_asin = keepa_client.stage2_full(db, batch)
            for asin in batch:
                stage2 = stage2_by_asin.get(asin)
                ean = stage1_survivors[asin]
                row = by_ean[ean]
                if stage2 is None:
                    continue

                dims = None
                if stage2.package_weight_kg and stage2.package_longest_cm and stage2.package_dims_sum_cm:
                    dims = SizeDims(stage2.package_weight_kg, stage2.package_longest_cm, stage2.package_dims_sum_cm)
                fees = fee_provider.get_fees(
                    stage2.category or "", stage2.buybox_price_pence or stage2.lowest_fba_offer_pence or 0, dims,
                    stage2.fba_fulfilment_fee_pence, stage2.referral_fee_percentage, asin=asin,
                )
                oversize = fee_provider.classify_size_tier(dims) == "oversize"

                category_rank_percentile = None
                if stage2.leaf_category_id is not None and stage2.leaf_category_rank is not None:
                    category_size = keepa_client.get_category_size(db, stage2.leaf_category_id)
                    if category_size:
                        category_rank_percentile = stage2.leaf_category_rank / category_size

                # --- pack size: price what the ASIN actually sells ---
                # The feed sells one unit; the ASIN may be a multipack. Buying
                # N singles and shipping them as a set is ordinary FBA
                # bundling, so a mismatch is not a reject -- it is a different
                # cost base. Score it at N x the unit price and let the
                # economics decide, rather than crediting a 6-pack's sale
                # price against one unit's cost (which is what produced
                # Pharmazon's phantom 300-550% ROIs). Unknown ASIN pack size
                # means unknown cost, so score at face value but say so.
                feed_mult = _pack_multiplier(row.name)
                asin_mult = _pack_multiplier(stage2.title)
                units_per_sale = _bundle_units(feed_mult, asin_mult)

                score_input = ScoreInput(
                    buy_price_pence=row.buy_price_pence * units_per_sale,
                    match_confidence="high",   # EAN match, same confidence tier as jsonld in pipeline.py
                    category=stage2.category or "",
                    fba_offer_count=stage2.fba_offer_count,
                    amazon_on_listing=stage2.amazon_on_listing,
                    fees=fees,
                    sales_rank=stage2.sales_rank,
                    est_monthly_sales=stage2.est_monthly_sales,
                    est_monthly_sales_source=stage2.est_monthly_sales_source,
                    buybox_price_pence=stage2.buybox_price_pence,
                    lowest_fba_offer_pence=stage2.lowest_fba_offer_pence,
                    buybox_avg_90d_pence=stage2.buybox_avg_90d_pence,
                    rank_history_days=stage2.rank_history_days,
                    hazmat=stage2.hazmat,
                    oversize=oversize,
                    gated=None,   # checked separately below, reported not filtered
                    category_rank_percentile=category_rank_percentile,
                    distinct_sellers_ever=stage2.distinct_sellers_ever,
                    max_new_offers_ever=stage2.max_new_offers_ever,
                )
                result = score_deal(score_input, cfg)
                flags = list(result.flags)
                if asin_mult is None:
                    flags.append(
                        "pack_size_unknown: no Amazon title to read a pack size from, "
                        "scored as 1 unit per sale -- confirm on the listing before ordering"
                    )
                elif units_per_sale > 1:
                    flags.append(
                        f"requires_bundling: {units_per_sale} units per sale "
                        f"(Amazon title {stage2.title!r} implies x{asin_mult}, feed unit implies "
                        f"x{feed_mult}) -- costed at {units_per_sale}x, and needs prep "
                        "(poly-bag, label, suffocation warning) not priced in here"
                    )
                elif asin_mult != feed_mult:
                    flags.append(
                        f"pack_size_mismatch: feed name implies x{feed_mult}, "
                        f"Amazon title ({stage2.title!r}) implies x{asin_mult} -- not a whole "
                        "multiple, so NOT repriced; check the listing by hand"
                    )
                entry = {
                    "asin": asin, "ean": ean, "brand": row.brand, "name": row.name,
                    "title": stage2.title,
                    "buy_price_pence": row.buy_price_pence, "list_price": row.list_price, "note": row.note,
                    "units_per_sale": units_per_sale,
                    "bundle_cost_pence": row.buy_price_pence * units_per_sale,
                    "sell_price_pence": result.sell_price_pence, "net_profit_pence": result.net_profit_pence,
                    "roi": result.roi, "sales_rank": stage2.sales_rank, "fba_offer_count": stage2.fba_offer_count,
                    # Velocity is the single biggest reject reason across every
                    # scan run so far, and until 2026-09-09 the number behind it
                    # survived only as prose inside verdict_reason — so "which
                    # products actually sell" needed re-querying Keepa for data
                    # already paid for. Source matters as much as the figure:
                    # keepa_confirmed is a real monthlySold badge, rank_drop_proxy
                    # is a noisy stand-in the gate trusts far less.
                    "est_monthly_sales": stage2.est_monthly_sales,
                    "est_monthly_sales_source": stage2.est_monthly_sales_source,
                    "verdict": result.verdict.value, "verdict_reason": result.verdict_reason, "flags": flags,
                }
                ckpt.write(json.dumps(entry) + "\n")
                results.append(entry)
            ckpt.flush()
            print(f"[SCAN:{source_name}] stage2 {min(i + CHUNK, len(survivor_asins))}/{len(survivor_asins)} new")

    passes = [r for r in results if r["verdict"] != Verdict.REJECT.value]
    print(f"[SCAN:{source_name}] {len(passes)} PASS/PASS_WITH_FLAGS out of {len(results)} scored")
    if check_gating:
        print(f"[SCAN:{source_name}] checking gating on PASS items...")

    reject_reasons: dict[str, int] = {}
    for r in results:
        if r["verdict"] == Verdict.REJECT.value:
            key = (r["verdict_reason"] or "unknown").split(";")[0].split(" ")[0]
            reject_reasons[key] = reject_reasons.get(key, 0) + 1

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"{source_name} scan -- {len(by_ean)} rows checked, {stage1_matched} matched Keepa, "
                f"{len(results)} reached full scoring, {len(passes)} PASS/PASS_WITH_FLAGS\n\n")
        for r in sorted(passes, key=lambda r: -(r["roi"] or 0)):
            if check_gating:
                time.sleep(_GATING_DELAY_S)
            g = spapi_client.check_gating_detail(db, r["asin"]) if check_gating else None
            # "gated True" hides the only distinction that matters when deciding
            # what to order: APPROVAL_REQUIRED is a trade invoice away,
            # NOT_ELIGIBLE is dead. Record the reason code, not a bool.
            gated = "unchecked" if g is None else ((g.reason_code or "yes") if g.gated else "no")
            approval = f"\n  apply: {g.approval_url}" if g and g.approval_url else ""
            note = f" ({r['note']})" if r["note"] else ""
            # Report the cost actually scored. For a bundle that is N units,
            # not one -- printing the unit price next to a multipack's sale
            # price is exactly the juxtaposition that made losing candidates
            # look like 500% ROI.
            units = r.get("units_per_sale", 1)
            cost_pence = r.get("bundle_cost_pence", r["buy_price_pence"])
            cost_str = f"{cost_pence/100:.2f}"
            if units > 1:
                cost_str += f" ({units} x {r['buy_price_pence']/100:.2f})"
            f.write(
                f"{r['brand']} | {r['name']}\n"
                f"  ASIN {r['asin']} | EAN {r['ean']} | cost {cost_str} "
                f"(list {r['list_price']:.2f}{note})\n"
                f"  sell {r['sell_price_pence']/100:.2f} | net_profit {r['net_profit_pence']/100:.2f} "
                f"| roi {r['roi']:.1%} | rank {r['sales_rank']} | offers {r['fba_offer_count']} "
                f"| verdict {r['verdict']} | gated {gated} | flags {r['flags']}\n"
                f"  https://www.amazon.co.uk/dp/{r['asin']}{approval}\n\n"
            )
        f.write(f"\nReject reasons (stage2 scored, {len(results) - len(passes)} total):\n")
        for k, v in sorted(reject_reasons.items(), key=lambda kv: -kv[1]):
            f.write(f"  {k}: {v}\n")

    print(f"[SCAN:{source_name}] wrote {len(passes)} candidates to {out_path}")

    # The checkpoints are the resume mechanism; this is the queryable record.
    # Best-effort on purpose: a scan that found deals must not report failure
    # because the database was unreachable. The .jsonl files still hold
    # everything and tools/backfill_scan_results.py can replay them.
    try:
        stage1_records = list(_load_checkpoint(stage1_checkpoint_path, "ean").values())
        written, _ = scan_store.persist_scan(db, source_name, stage1_records, results)
        print(f"[SCAN:{source_name}] persisted {written} rows to supplier_scan_results")
    except Exception as e:   # noqa: BLE001 -- reporting only, the scan itself succeeded
        print(f"[SCAN:{source_name}] WARNING: could not persist to DB "
              f"({type(e).__name__}: {e}); checkpoints intact, "
              f"replay with tools/backfill_scan_results.py")
