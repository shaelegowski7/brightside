"""Runner: scores a hand-supplied list of ASINs against the same gates every
other source runs (app/decision/engine.py), with no supplier feed involved.

Why this is separate from wholesale_scan.py: that engine starts from an EAN
and spends a Keepa call resolving EAN -> ASIN, then a cheap stage1 screen so
it doesn't pay for stage2 on thousands of catalogue rows. Neither applies
here -- the ASIN *is* the input, and a hand-picked list is tens of items, not
thousands, so stage1's token saving isn't worth the extra call. This goes
straight to stage2 (offers=20, ~13 tokens/item) and reports every ASIN, PASS
or REJECT: on a hand-picked list the reason for each rejection is the point,
where wholesale_scan deliberately only lists survivors.

Two input modes:

  buy price given  -- full verdict from score_deal: ROI, net profit, flags.
  no buy price     -- structural gates only (Amazon on listing, FBA offer
                      count, rank, velocity, hazmat, oversize), plus the
                      highest price we could buy at and still clear both
                      thresholds, via candidate_finder.target_buy_price_pence.
                      Same "shopping list" answer as the reverse finder.

Structural-only mode scores at that target price so the real engine still
decides every gate -- there is no second copy of the gate logic here to drift
out of sync with engine.py. A target of 0 means the ASIN can't clear the
thresholds at any purchase price, not even free; scoring it at 1p reports
that as the ROI rejection it genuinely is.

Gating is reported, never filtered on (same convention as wholesale_scan.py
and candidate_finder.py). Locally it is always None -- the SPAPI_* creds live
only in Railway -- so for real gating status run:

    railway run -- "C:\\Users\\shael\\brightside\\.venv\\Scripts\\python.exe" tools/scan_asin_list.py asins.txt

Railway then injects an internal-only DATABASE_URL, which _use_local_db()
overrides back to .env's public proxy URL for the same database.

Usage:
    python tools/scan_asin_list.py B0CSZ82WLH B00HER8E5A
    python tools/scan_asin_list.py asins.txt
    python tools/scan_asin_list.py asins.txt --price 4.99   # applies to rows with no price of their own

Input file: one ASIN per line, `#` comments and blank lines ignored, with an
optional buy price (GBP, the real inc-VAT cash cost per unit) and optional
name after it:

    B0CSZ82WLH                          # no price -> target-price mode
    B00HER8E5A, 4.99
    B0BXX8X7DM, 12.50, DEWALT multi-tool

Not checkpointed, unlike wholesale_scan.py: a list this size is one Keepa
batch, so an interrupted run costs seconds rather than hours of paid lookups.
"""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_ASIN_RE = re.compile(r"^B[0-9A-Z]{9}$")
_REPO_ROOT = Path(__file__).resolve().parent.parent
_OUT_PATH = _REPO_ROOT / "asin_check_candidates.txt"
_CHUNK = 100


def _use_local_db() -> None:
    """Under `railway run`, the injected DATABASE_URL is postgres.railway
    .internal, which doesn't resolve outside Railway's own network. .env holds
    the public proxy URL for that same database -- prefer it, while keeping
    every other injected var (the SPAPI_* ones are the whole point of running
    under railway).
    """
    from dotenv import dotenv_values, load_dotenv

    load_dotenv()
    local_url = dotenv_values(_REPO_ROOT / ".env").get("DATABASE_URL")
    if local_url:
        os.environ["DATABASE_URL"] = local_url


def parse_input(args: list[str], default_price_pence: int | None) -> list[tuple[str, int | None, str]]:
    """Returns [(asin, buy_price_pence|None, name)]. Each arg is either a path
    to a list file or a bare ASIN typed on the command line."""
    lines: list[str] = []
    for arg in args:
        path = Path(arg)
        if path.exists():
            # Some scratch files in this repo are cp1252, not UTF-8 (£ = 0xa3).
            lines.extend(path.read_text(encoding="utf-8", errors="replace").splitlines())
        else:
            lines.append(arg)

    rows: list[tuple[str, int | None, str]] = []
    seen: set[str] = set()
    for raw in lines:
        line = raw.split("#")[0].strip()
        if not line:
            continue
        sep = ", " if "," in line else " "
        parts = [p.strip() for p in (line.split(",") if "," in line else line.split())]
        asin = parts[0].upper()
        if not _ASIN_RE.match(asin):
            print(f"[ASIN-CHECK] skipping unparseable line: {raw.strip()!r}")
            continue
        if asin in seen:
            continue
        seen.add(asin)

        price_pence = default_price_pence
        name_parts = parts[1:]
        if name_parts:
            try:
                price_pence = round(float(name_parts[0].lstrip("£").replace(",", "")) * 100)
                name_parts = name_parts[1:]
            except ValueError:
                pass   # not a price, so it's part of the name
        rows.append((asin, price_pence, sep.join(name_parts)))
    return rows


def _format_entry(asin, stage2, name, buy_price_pence, result, cfg, costs, sell_price, target,
                  category_rank_percentile, gating_status) -> str:
    pct = f"{category_rank_percentile:.3%}" if category_rank_percentile is not None else "n/a"
    avg90 = f"{stage2.buybox_avg_90d_pence / 100:.2f}" if stage2.buybox_avg_90d_pence else "n/a"
    price_source = "min(buybox, 90d avg)" if stage2.buybox_price_pence else "no buybox, lowest FBA"
    amazon_stock = (f"{stage2.amazon_instock_pct_90:.0%}" if stage2.amazon_instock_pct_90 is not None else "n/a")

    lines = [
        f"{asin} | {stage2.title or name or '?'}",
        f"  category {stage2.category!r} | rank 90d avg {stage2.sales_rank_avg90} (leaf pct {pct})"
        f" | offers {stage2.fba_offer_count} | est sales/mo {stage2.est_monthly_sales}"
        f" | amazon_on_listing {stage2.amazon_on_listing} (in stock {amazon_stock} of 90d)"
        f" | gating {gating_status or 'unchecked'}",
        f"  sell {sell_price / 100:.2f} ({price_source}, 90d avg {avg90})"
        f" | fees {costs['total_fees_pence'] / 100:.2f} + storage {costs['storage_cost_pence'] / 100:.2f}"
        f" ({costs['est_months_to_sell']:.1f}mo) + returns {costs['returns_allowance_pence'] / 100:.2f}"
        f" + prep {costs['prep_cost_pence'] / 100:.2f} + inbound {costs['inbound_shipping_pence'] / 100:.2f}",
    ]
    if buy_price_pence is not None:
        if result.net_profit_pence is None:
            # A hard gate fired, so engine.py never reached the financials --
            # say so rather than printing "n/a" and looking like a data gap.
            lines.append(f"  cost {buy_price_pence / 100:.2f} -> not costed "
                         f"(rejected by a hard gate before the money maths)")
        else:
            roi = f"{result.roi:.1%}" if result.roi is not None else "n/a"
            lines.append(f"  cost {buy_price_pence / 100:.2f} -> "
                         f"net_profit {result.net_profit_pence / 100:.2f} | roi {roi}")
    if target > 0:
        discount = 1 - (target / sell_price) if sell_price else 0.0
        lines.append(f"  buy at or below {target / 100:.2f} to clear {cfg.min_roi:.0%} ROI"
                     f" + {cfg.min_net_profit_pence}p net ({discount:.0%} off the sell price)")
    else:
        lines.append("  cannot clear the thresholds at ANY buy price, not even free")

    verdict = f"  {result.verdict.value}"
    if result.verdict_reason:
        verdict += f" -- {result.verdict_reason}"
    if result.velocity_basis:
        verdict += f" | velocity via {result.velocity_basis}"
    if result.flags:
        verdict += f" | flags {result.flags}"
    if buy_price_pence is None:
        verdict += "   (structural gates only -- scored at the target buy price)"
    lines.append(verdict)
    lines.append(f"  https://www.amazon.co.uk/dp/{asin}")
    return "\n".join(lines) + "\n"


def run(rows: list[tuple[str, int | None, str]]) -> None:
    from app import keepa_client, spapi_client
    from app.assessment import assess
    from app.config import get_config
    from app.database import SessionLocal
    from app.decision.engine import DecisionConfig, Verdict
    from app.pricing.fees import build_fee_provider

    db = SessionLocal()
    app_cfg = get_config()
    cfg = DecisionConfig.from_app_config(app_cfg)
    fee_provider = build_fee_provider(db, app_cfg)
    check_gating = spapi_client.is_configured()
    print(f"[ASIN-CHECK] {len(rows)} ASIN(s); gating check "
          f"{'enabled' if check_gating else 'disabled (no local SP-API creds)'}")

    by_asin = {r[0]: r for r in rows}
    asins = [r[0] for r in rows]
    stage2_by_asin: dict = {}
    for i in range(0, len(asins), _CHUNK):
        batch = asins[i:i + _CHUNK]
        stage2_by_asin.update(keepa_client.stage2_full(db, batch))
        print(f"[ASIN-CHECK] stage2 {min(i + _CHUNK, len(asins))}/{len(asins)}")

    report: list[str] = []
    passes = 0
    for asin in asins:
        _, buy_price_pence, name = by_asin[asin]
        stage2 = stage2_by_asin.get(asin)
        if stage2 is None:
            report.append(f"{asin} | {name or '?'}\n  NOT FOUND on Keepa\n")
            continue

        a = assess(db, stage2, cfg, fee_provider, buy_price_pence, check_gating)
        if a.result.verdict != Verdict.REJECT:
            passes += 1

        report.append(_format_entry(
            asin, stage2, name, buy_price_pence, a.result, cfg, a.costs, a.sell_price_pence,
            a.target_buy_price_pence, a.category_rank_percentile, a.gating_status,
        ))

    body = (f"asin_check -- {len(rows)} ASIN(s) checked, {len(stage2_by_asin)} found on Keepa, "
            f"{passes} PASS/PASS_WITH_FLAGS\n\n" + "\n".join(report))
    _OUT_PATH.write_text(body, encoding="utf-8")
    print("\n" + body)
    print(f"[ASIN-CHECK] wrote {_OUT_PATH}")


if __name__ == "__main__":
    argv = sys.argv[1:]
    default_price_pence = None
    if "--price" in argv:
        idx = argv.index("--price")
        default_price_pence = round(float(argv[idx + 1].lstrip("£").replace(",", "")) * 100)
        argv = argv[:idx] + argv[idx + 2:]
    if not argv:
        print(__doc__)
        sys.exit(1)
    _use_local_db()
    rows = parse_input(argv, default_price_pence)
    if not rows:
        print("[ASIN-CHECK] no usable ASINs in input")
        sys.exit(1)
    run(rows)
