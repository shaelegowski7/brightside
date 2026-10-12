"""Sourcing lookup for every pass in a Keepa scan checkpoint, written as one
JSON file (for the results artifact). Keepa data comes from the cache the
scan just filled, so this spends no Keepa tokens; run under Railway for the
ScraperAPI key and SP-API gating:

    railway run -- C:/Users/shael/brightside/.venv/Scripts/python.exe tools/source_batch.py <checkpoint.jsonl> <out.json>
"""
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.scan_asin_list import _use_local_db  # noqa: E402
from tools.source_asin import gather_leads, load_vault  # noqa: E402

# A ScraperAPI Google search takes ~13s and a product needs ~2.3 new ones
# (2026-10-10), so one-at-a-time ran at 2 products a minute, nearly all of
# it waiting. Brand groups run in parallel; one brand stays on one worker
# because the search cache is a file per brand and two writers would drop
# each other's results.
_WORKERS = 5


def main(ckpt: Path, out: Path) -> None:
    _use_local_db()
    from app import keepa_cache, keepa_client, sourcing, spapi_client
    from app.config import get_settings
    from app.database import SessionLocal

    key = get_settings().scraperapi_key
    gating_on = spapi_client.is_configured()
    vault = load_vault()

    candidates = [c for line in ckpt.read_text(encoding="utf-8").splitlines()
                  for c in json.loads(line)["candidates"]]
    db = SessionLocal()
    stage2s = keepa_cache.get(db, 2, [c["asin"] for c in candidates], keepa_cache.max_age("market", 30),
                              keepa_client.Stage2Result)
    db.close()
    print(f"[BATCH] {len(candidates)} passes, {len(stage2s)} with cached Keepa data; gating {'on' if gating_on else 'off'}")

    groups: dict[str, list[dict]] = {}
    for c in candidates:
        s2 = stage2s.get(c["asin"])
        groups.setdefault(sourcing.norm(s2.brand) if s2 and s2.brand else c["asin"], []).append(c)

    rows, lock = [], threading.Lock()

    def source_group(group: list[dict]) -> None:
        db = SessionLocal()
        try:
            for c in group:
                s2 = stage2s.get(c["asin"])
                gate = None
                if gating_on:
                    time.sleep(0.3)
                    gate = spapi_client.check_gating_detail(db, c["asin"])
                report = sourcing.SourcingReport(asin=c["asin"], title=s2.title if s2 else c["title"],
                                                 brand=s2.brand if s2 else None, eans=(s2.eans or []) if s2 else [])
                gather_leads(db, report, report.title, sourcing.no_route_signals_from_stage2(s2) if s2 else [],
                             key, set(), vault)
                row = {
                    **c,
                    "keepa_title": report.title,
                    "brand": report.brand,
                    "eans": report.eans,
                    "category": s2.category if s2 else None,
                    "sales_rank_avg90": s2.sales_rank_avg90 if s2 else None,
                    "gating": spapi_client.gating_status(gate),
                    "approval_url": gate.approval_url if gate else None,
                    "verdict": report.verdict,
                    "no_route": report.no_route_signals,
                    "board_note": report.board_note,
                    "leads": [asdict(l) for l in report.leads],
                }
                with lock:
                    rows.append(row)
                    n = len(rows)
                    if n % 10 == 0:
                        # partial file so the artifact can show sourcing before the batch ends
                        out.write_text(json.dumps(rows, indent=1), encoding="utf-8")
                        print(f"[BATCH] {n}/{len(candidates)}", flush=True)
        finally:
            db.close()

    with ThreadPoolExecutor(_WORKERS) as pool:
        for f in as_completed([pool.submit(source_group, g) for g in groups.values()]):
            f.result()
    out.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    print(f"[BATCH] wrote {out}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(Path(sys.argv[1]), Path(sys.argv[2]))
