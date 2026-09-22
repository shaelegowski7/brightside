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
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from app import keepa_client, spapi_client
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
    reruns of the same supplier's list so checkpoints keep matching up."""
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

                score_input = ScoreInput(
                    buy_price_pence=row.buy_price_pence,
                    match_confidence="high",   # EAN match, same confidence tier as jsonld in pipeline.py
                    category=stage2.category or "",
                    fba_offer_count=stage2.fba_offer_count,
                    amazon_on_listing=stage2.amazon_on_listing,
                    fees=fees,
                    sales_rank=stage2.sales_rank,
                    est_monthly_sales=stage2.est_monthly_sales,
                    buybox_price_pence=stage2.buybox_price_pence,
                    lowest_fba_offer_pence=stage2.lowest_fba_offer_pence,
                    buybox_avg_90d_pence=stage2.buybox_avg_90d_pence,
                    rank_history_days=stage2.rank_history_days,
                    hazmat=stage2.hazmat,
                    oversize=oversize,
                    gated=None,   # checked separately below, reported not filtered
                    category_rank_percentile=category_rank_percentile,
                )
                result = score_deal(score_input, cfg)
                entry = {
                    "asin": asin, "ean": ean, "brand": row.brand, "name": row.name,
                    "buy_price_pence": row.buy_price_pence, "list_price": row.list_price, "note": row.note,
                    "sell_price_pence": result.sell_price_pence, "net_profit_pence": result.net_profit_pence,
                    "roi": result.roi, "sales_rank": stage2.sales_rank, "fba_offer_count": stage2.fba_offer_count,
                    "verdict": result.verdict.value, "verdict_reason": result.verdict_reason, "flags": result.flags,
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
            gated = spapi_client.check_gating(db, r["asin"]) if check_gating else None
            note = f" ({r['note']})" if r["note"] else ""
            f.write(
                f"{r['brand']} | {r['name']}\n"
                f"  ASIN {r['asin']} | EAN {r['ean']} | cost {r['buy_price_pence']/100:.2f} "
                f"(list {r['list_price']:.2f}{note})\n"
                f"  sell {r['sell_price_pence']/100:.2f} | net_profit {r['net_profit_pence']/100:.2f} "
                f"| roi {r['roi']:.1%} | rank {r['sales_rank']} | offers {r['fba_offer_count']} "
                f"| verdict {r['verdict']} | gated {gated} | flags {r['flags']}\n"
                f"  https://www.amazon.co.uk/dp/{r['asin']}\n\n"
            )
        f.write(f"\nReject reasons (stage2 scored, {len(results) - len(passes)} total):\n")
        for k, v in sorted(reject_reasons.items(), key=lambda kv: -kv[1]):
            f.write(f"  {k}: {v}\n")

    print(f"[SCAN:{source_name}] wrote {len(passes)} candidates to {out_path}")
