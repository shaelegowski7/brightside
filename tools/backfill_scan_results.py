"""Replay existing {supplier}_stage{1,2}_checkpoint.jsonl files into
supplier_scan_results.

Six suppliers were scanned before that table existed (2026-09-09): 15,416
stage-1 lookups and 2,578 full scorings whose Keepa tokens are already spent
and whose only copy was untracked .jsonl in the repo root. This loads them
without re-querying anything.

Also the recovery path if a live scan's DB write fails — persist_scan is an
upsert keyed on (supplier, ean), so re-running this is safe and idempotent.

    python tools/backfill_scan_results.py            # every supplier found
    python tools/backfill_scan_results.py honeypot   # just one
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import scan_store
from app.database import SessionLocal

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.open(encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            # A checkpoint truncated mid-write by a killed scan: the last
            # line can be a partial record. Everything before it is fine.
            print(f"  ! skipping malformed line in {path.name}")
    return out


def suppliers() -> list[str]:
    return sorted(p.name.replace("_stage1_checkpoint.jsonl", "")
                  for p in _REPO_ROOT.glob("*_stage1_checkpoint.jsonl"))


def main(names: list[str]) -> None:
    db = SessionLocal()
    total = 0
    for name in names:
        s1 = _read(_REPO_ROOT / f"{name}_stage1_checkpoint.jsonl")
        s2 = _read(_REPO_ROOT / f"{name}_stage2_checkpoint.jsonl")
        # Dedupe on EAN, keeping the last record — a resumed scan appends
        # rather than rewriting, so the same EAN can appear more than once.
        s1 = list({r["ean"]: r for r in s1 if r.get("ean")}.values())
        s2 = list({r["ean"]: r for r in s2 if r.get("ean")}.values())
        written, _ = scan_store.persist_scan(db, name, s1, s2)
        total += written
        print(f"{name:<16} stage1 {len(s1):>6}  stage2 {len(s2):>5}  ->  {written:>6} rows")
    print(f"\n{total} rows in supplier_scan_results")


if __name__ == "__main__":
    main(sys.argv[1:] or suppliers())
