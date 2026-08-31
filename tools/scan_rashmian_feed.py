"""One-off: scans the Rashmian wholesale feed (RashmianFeedExcel.xls) against
the same stage1 -> stage2 -> score_deal pipeline every scraper source runs
(see app/pipeline.py), just fed from a spreadsheet's TRADE PRICE column
instead of a scraped retailer price. Answers "which of Rashmian's products
would actually clear brightside's ROI/profit bar on Amazon today?"

TRADE PRICE is confirmed EX-VAT (rashmian.com/conditions-of-use: "All prices
on our website are exclusive of VAT"). Brightside is not VAT-registered
(config.yaml vat_registered: false), so VAT paid on purchase is a real,
non-reclaimable cost -- buy_price_pence below is TRADE PRICE * 1.20, the
actual cash outlay per unit. Same "use the inc-VAT figure" convention as
app/sources/nda_toys.py.

Gating is computed and reported per-PASS item but NOT used to filter, same
precedent as the reverse candidate search (candidates_2026-08-29.txt):
gating status is worth knowing, not worth silently discarding a real
financial match over, since APPROVAL_REQUIRED categories are approvable.
Locally this is moot anyway -- SP-API creds live on Railway only, so
spapi_client.is_configured() is False here and gating comes back None.

Batches Keepa lookups by EAN (up to 100/call, matching keepa_client's own
batch limit) and maps each result back to its source row via the product's
eanList field, since a batch product query does not otherwise expose which
input code produced which match.
"""
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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

FEED_PATH = r"C:\Users\shael\Downloads\RashmianFeedExcel.xls"
OUT_PATH = Path(__file__).resolve().parent.parent / "rashmian_candidates.txt"
CHUNK = 100
_GATING_DELAY_S = 0.3

# First run only checked exact "In Stock" -- silently dropped 64 rows
# marked "In Stock - Limited Stock Available", a distinct status string.
_AVAILABILITY_PRESETS = {
    "in_stock": {"In Stock"},
    "limited": {"In Stock - Limited Stock Available"},
    "all": {"In Stock", "In Stock - Limited Stock Available"},
}


@dataclass
class FeedRow:
    ean: str
    brand: str
    name: str
    category: str
    buy_price_pence: int   # inc-VAT, real cash cost
    trade_price_ex_vat: float
    multiple: int


def load_feed(path: str, availability_values: set[str]) -> list[FeedRow]:
    df = pd.read_excel(path)
    df = df[df["Availability"].isin(availability_values)]
    df = df[df["Barcode"].notna() & df["TRADE PRICE"].notna()]
    rows: list[FeedRow] = []
    seen: set[str] = set()
    for _, r in df.iterrows():
        ean = str(int(r["Barcode"]))
        if len(ean) not in (12, 13) or ean in seen:
            continue
        seen.add(ean)
        trade_price = float(r["TRADE PRICE"])
        multiple = r["Sold in Multiples of"]
        rows.append(FeedRow(
            ean=ean,
            brand=str(r["Brand"]),
            name=str(r["Product Name"]),
            category=str(r["Category"]),
            buy_price_pence=round(trade_price * 1.20 * 100),
            trade_price_ex_vat=trade_price,
            multiple=int(multiple) if pd.notna(multiple) else 1,
        ))
    return rows


def stage1_by_ean(db, eans_batch: list[str]) -> dict[str, tuple[str, Stage1Result]]:
    """Same query stage1_screen makes, but also returns eanList so results
    can be mapped back to the specific input row (stage1_screen only keys
    by ASIN, which is fine when caller already knows the ASIN, but useless
    here where we start from the EAN)."""
    client = _get_client()
    tokens_before = client.tokens_left
    products = client.query(
        eans_batch, domain=KEEPA_DOMAIN, stats=90, offers=None,
        product_code_is_asin=False, wait=True,
    )
    _log_tokens(db, "rashmian_stage1", len(eans_batch), tokens_before, client.tokens_left)

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


def main(availability_values: set[str] = _AVAILABILITY_PRESETS["in_stock"], out_path: Path = OUT_PATH) -> None:
    rows = load_feed(FEED_PATH, availability_values)
    by_ean = {r.ean: r for r in rows}
    print(f"[SCAN] {len(rows)} usable rows ({sorted(availability_values)}, barcode, trade price)")

    db = SessionLocal()
    app_cfg = get_config()
    cfg = DecisionConfig.from_app_config(app_cfg)
    fee_provider = build_fee_provider(db, app_cfg)
    check_gating = spapi_client.is_configured()
    print(f"[SCAN] gating check {'enabled' if check_gating else 'disabled (no local SP-API creds)'}")

    eans = list(by_ean.keys())
    stage1_matched = 0
    stage1_pass = 0
    stage1_survivors: dict[str, str] = {}   # asin -> ean

    for i in range(0, len(eans), CHUNK):
        batch = eans[i:i + CHUNK]
        matched = stage1_by_ean(db, batch)
        stage1_matched += len(matched)
        for ean, (asin, s1) in matched.items():
            row = by_ean[ean]
            passes, reason = keepa_client.stage1_screen_passes(s1, row.buy_price_pence, cfg, fee_provider)
            if passes:
                stage1_pass += 1
                stage1_survivors[asin] = ean
        done = min(i + CHUNK, len(eans))
        print(f"[SCAN] stage1 {done}/{len(eans)} -- {stage1_matched} matched on Keepa, "
              f"{stage1_pass} survive the optimistic screen")

    print(f"[SCAN] stage1 done: {stage1_matched}/{len(eans)} matched a Keepa product, "
          f"{stage1_pass} survive the cheap screen -- running stage2 on those")

    results = []
    survivor_asins = list(stage1_survivors.keys())
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
            results.append((row, stage2, result))
        print(f"[SCAN] stage2 {min(i + CHUNK, len(survivor_asins))}/{len(survivor_asins)}")

    passes = [(row, s2, r) for row, s2, r in results if r.verdict != Verdict.REJECT]
    print(f"[SCAN] {len(passes)} PASS/PASS_WITH_FLAGS out of {len(results)} scored")
    if check_gating:
        print("[SCAN] checking gating on PASS items...")

    reject_reasons: dict[str, int] = {}
    for _, _, r in results:
        if r.verdict == Verdict.REJECT:
            key = (r.verdict_reason or "unknown").split(";")[0].split(" ")[0]
            reject_reasons[key] = reject_reasons.get(key, 0) + 1

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"Rashmian feed scan -- {len(rows)} rows checked, {stage1_matched} matched Keepa, "
                f"{len(results)} reached full scoring, {len(passes)} PASS/PASS_WITH_FLAGS\n\n")
        for row, s2, r in sorted(passes, key=lambda t: -(t[2].roi or 0)):
            if check_gating:
                time.sleep(_GATING_DELAY_S)
            gated = spapi_client.check_gating(db, s2.asin) if check_gating else None
            f.write(
                f"{row.brand} | {row.name}\n"
                f"  ASIN {s2.asin} | EAN {row.ean} | cost {row.buy_price_pence/100:.2f} "
                f"(trade {row.trade_price_ex_vat:.2f} ex-VAT, x{row.multiple} multiple)\n"
                f"  sell {r.sell_price_pence/100:.2f} | net_profit {r.net_profit_pence/100:.2f} "
                f"| roi {r.roi:.1%} | rank {s2.sales_rank} | offers {s2.fba_offer_count} "
                f"| verdict {r.verdict.value} | gated {gated} | flags {r.flags}\n"
                f"  https://www.amazon.co.uk/dp/{s2.asin}\n\n"
            )
        f.write(f"\nReject reasons (stage2 scored, {len(results) - len(passes)} total):\n")
        for k, v in sorted(reject_reasons.items(), key=lambda kv: -kv[1]):
            f.write(f"  {k}: {v}\n")

    print(f"[SCAN] wrote {len(passes)} candidates to {out_path}")


if __name__ == "__main__":
    preset = sys.argv[1] if len(sys.argv) > 1 else "in_stock"
    values = _AVAILABILITY_PRESETS[preset]
    out = Path(__file__).resolve().parent.parent / f"rashmian_candidates_{preset}.txt" if preset != "in_stock" else OUT_PATH
    main(values, out)
