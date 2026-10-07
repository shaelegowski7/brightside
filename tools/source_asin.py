"""Where can I buy this ASIN? See app/sourcing.py for the evidence order.

Run under Railway so the ScraperAPI key and SP-API gating credentials are
available (the DB URL is switched back to the public one, as in
scan_asin_list.py):

    railway run -- C:/Users/shael/brightside/.venv/Scripts/python.exe tools/source_asin.py B003RA2TBS

Options:
    --no-web    offline evidence only (scans, board, supplier list, Keepa)
    --web       search the web even when known suppliers were found
    --refresh   ignore the brand's cached web results

Web results are cached per brand for 30 days in the vault's
Sourcing/cache/, and every report is written to Sourcing/<date> <ASIN>.md.
"""
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.scan_asin_list import _use_local_db  # noqa: E402

_VAULT = Path(os.environ.get("BRIGHTSIDE_VAULT", r"C:\Users\shael\brainz\Projects\Brightside"))
_SOURCING = _VAULT / "Sourcing"
_CACHE_TTL = timedelta(days=30)


def _cached_search(search, key: str, refresh: bool):
    from app.sourcing import norm
    path = _SOURCING / "cache" / f"{norm(key) or 'unknown'}.json"
    cache = {}
    if path.exists() and not refresh:
        cache = json.loads(path.read_text(encoding="utf-8"))
        if datetime.fromisoformat(cache.get("fetched", "2000-01-01")) < datetime.now() - _CACHE_TTL:
            cache = {}
    results = cache.get("results", {})
    spent = []

    def run(query: str) -> list[dict]:
        if query not in results:
            results[query] = search(query)
            spent.append(query)
        return results[query]

    def save():
        if spent:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"fetched": cache.get("fetched") or datetime.now().isoformat(timespec="seconds"),
                                        "results": results}, indent=1), encoding="utf-8")
    return run, save, spent


def main(argv: list[str]) -> None:
    flags = {a for a in argv if a.startswith("--")}
    asins = [a.upper() for a in argv if not a.startswith("--")]
    if len(asins) != 1:
        sys.exit(__doc__)
    asin = asins[0]

    _use_local_db()
    from app import keepa_client, sourcing, spapi_client
    from app.assessment import assess
    from app.config import get_config, get_settings
    from app.database import SessionLocal
    from app.decision.engine import DecisionConfig
    from app.pricing.fees import build_fee_provider
    from app.sources.scraperapi import google_search

    db = SessionLocal()
    app_cfg = get_config()
    cfg = DecisionConfig.from_app_config(app_cfg)

    products = keepa_client.fetch_full(db, [asin])
    if not products or not products[0].get("asin"):
        sys.exit(f"{asin}: not found on Keepa")
    product = products[0]
    stage2 = keepa_client.parse_stage2(product)
    a = assess(db, stage2, cfg, build_fee_provider(db, app_cfg), check_gating=spapi_client.is_configured())

    brand = product.get("brand") or product.get("manufacturer")
    eans = list(dict.fromkeys((product.get("eanList") or []) + (product.get("upcList") or [])))
    report = sourcing.SourcingReport(asin=asin, title=product.get("title"), brand=brand, eans=eans)

    board_path = _SOURCING / "wholesaler_board.json"
    board = json.loads(board_path.read_text(encoding="utf-8")) if board_path.exists() else {}
    master_path = _VAULT / "Supplier Master List.md"
    master = master_path.read_text(encoding="utf-8") if master_path.exists() else ""

    board_leads, report.board_note = sourcing.board_hits(board, asin, brand)
    leads = sourcing.scan_hits(db, asin, eans) + board_leads + sourcing.master_list_leads(master, brand)

    main_seller = keepa_client.main_seller_id(product)
    report.no_route_signals = sourcing.no_route_signals(
        product, keepa_client._seller_country_cache.get(main_seller), stage2.brand_matches_main_seller)

    key = get_settings().scraperapi_key
    known = [l for l in leads if l.kind in ("stocks_it", "known_supplier")]
    skip_web = "--no-web" in flags or (not {"--web"} & flags and (
        any(l.kind == "stocks_it" for l in leads) or len({l.name for l in known}) >= 2))
    if not skip_web and not key:
        print("[SOURCE] no SCRAPERAPI_KEY -- run under `railway run` for the web step; skipping it")
    elif not skip_web:
        search, save, spent = _cached_search(lambda q: google_search(q, key), brand or asin, "--refresh" in flags)
        leads += sourcing.web_leads(search, brand, eans, product.get("title"))
        save()
        print(f"[SOURCE] web: {len(spent)} new search(es), the rest from cache")

    deduped: dict[str, sourcing.Lead] = {}
    for lead in leads:
        k = sourcing.norm(lead.name)
        if not any(k and (k in other or other in k) for other in deduped):
            deduped[k] = lead
    for lead in deduped.values():
        lead.status = lead.status or sourcing.status_for(
            master, lead.name, (lead.url or "").split("/")[2] if lead.url else "")
    report.leads = list(deduped.values())
    report.verdict = sourcing.verdict(report)

    r = a.result
    assessment_lines = [
        f"Sells £{a.sell_price_pence / 100:.2f} · {f'{stage2.est_monthly_sales:.0f}' if stage2.est_monthly_sales else '?'} a month · "
        f"{stage2.fba_offer_count} FBA sellers · gating {a.gating_status or 'unchecked'}  ",
        (f"Buy at or below **£{a.target_buy_price_pence / 100:.2f}** for {cfg.min_roi:.0%} ROI and "
         f"£{cfg.min_net_profit_pence / 100:.2f} profit" if a.target_buy_price_pence
         else "Can't clear the thresholds at any buy price") +
        f" · scanner: {r.verdict.value}" + (f" ({r.verdict_reason})" if r.verdict_reason else ""),
    ]
    body = sourcing.render_markdown(report, assessment_lines)
    out = _SOURCING / f"{date.today():%Y-%m-%d} {asin}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8")
    print(body)
    print(f"[SOURCE] wrote {out}")


if __name__ == "__main__":
    main(sys.argv[1:])
