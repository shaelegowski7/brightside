"""Exports confirmed deals (dashboard.get_confirmed_deals) as one markdown
note per deal, keyed by ASIN, into an Obsidian vault folder. Meant to be run
locally by hand (or on a schedule via the OS, not this app's APScheduler --
the vault lives on the user's machine, not the server) with:

    python -m app.obsidian_export --vault "C:\\path\\to\\vault"

Frontmatter is regenerated every run so re-scored deals (ROI/profit change
as prices move) stay current, but anything the user writes below the
frontmatter block is preserved across reruns -- that's the whole point of
using per-deal notes instead of a single dumped table: the user can attach
their own supplier notes / links to other vault pages and have them survive
the next export.
"""
import argparse
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import dashboard  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.dashboard import DealRow  # noqa: E402

_DEFAULT_SUBDIR = "Brightside Deals"
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n?", re.DOTALL)
_NEW_NOTE_BODY = "\n## Notes\n\n"
_INDEX_FILENAME = "_Index.md"


def _slugify(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-") or "deal"


def _note_filename(row: DealRow) -> str:
    # ASIN is stable and unique, so keying on it makes the export idempotent
    # across reruns -- the same product always lands on the same note, and
    # other vault pages can link to it with [[ASIN]] and have that survive.
    stem = row.asin or _slugify(row.title)[:60]
    return f"{stem}.md"


def _to_pounds(pence: int | None) -> float | None:
    return round(pence / 100, 2) if pence is not None else None


def _build_frontmatter(row: DealRow) -> dict:
    return {
        "asin": row.asin,
        "title": row.title,
        "retailer": row.retailer,
        "retailer_url": row.retailer_url,
        "amazon_url": f"https://www.amazon.co.uk/dp/{row.asin}" if row.asin else None,
        "buy_price": _to_pounds(row.buy_price_pence),
        "sell_price": _to_pounds(row.sell_price_pence),
        "net_profit": _to_pounds(row.net_profit_pence),
        "roi_pct": round(row.roi * 100, 1) if row.roi is not None else None,
        "est_monthly_sales": row.est_monthly_sales,
        "match_confidence": row.match_confidence,
        "verdict": row.verdict,
        "flags": row.flags,
        "scored_at": row.ts.isoformat() if row.ts else None,
        "tags": ["brightside-deal"],
    }


def _existing_body(path: Path) -> str:
    if not path.exists():
        return _NEW_NOTE_BODY
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    return text[match.end():] if match else text


def render_note(row: DealRow, body: str) -> str:
    yaml_block = yaml.safe_dump(_build_frontmatter(row), sort_keys=False, allow_unicode=True).strip()
    return f"---\n{yaml_block}\n---\n{body}"


def dedupe_by_filename(rows: list[DealRow]) -> list[DealRow]:
    """Two different deals can resolve to the same ASIN -- the same product
    surfacing via two retailers/sources (see models.Ping's docstring, which
    is exactly why pings are keyed on ASIN, not deal_id). Notes are keyed on
    ASIN too, so without this, whichever row happened to be written last
    would silently clobber the other -- not necessarily the freshest score,
    since ordering is newest-first only overall, not per-ASIN. Keeping the
    first occurrence per filename keeps the newest score for that ASIN."""
    seen: dict[str, DealRow] = {}
    for row in rows:
        key = _note_filename(row)
        seen.setdefault(key, row)
    return list(seen.values())


def export_deals(rows: list[DealRow], vault_dir: Path) -> tuple[int, int]:
    """Writes/updates one note per row in vault_dir. Returns (created, updated)."""
    vault_dir.mkdir(parents=True, exist_ok=True)
    created = updated = 0
    for row in rows:
        path = vault_dir / _note_filename(row)
        existed = path.exists()
        body = _existing_body(path)
        path.write_text(render_note(row, body), encoding="utf-8")
        created += not existed
        updated += existed
    return created, updated


def _display(text: str) -> str:
    # Table cells and wikilinks both break on a literal "|" -- strip rather
    # than escape, since this is a display label only (the true title is
    # already in the per-deal note's own frontmatter).
    return text.replace("|", "-").replace("\n", " ").strip()


def _fmt_pence(pence: int | None) -> str:
    return f"£{pence / 100:,.2f}" if pence is not None else "—"


def _index_row(row: DealRow) -> str:
    stem = _note_filename(row)[:-len(".md")]
    roi = f"{row.roi * 100:.0f}%" if row.roi is not None else "—"
    return (
        f"| [[{stem}]] | {_display(row.title)} | {_display(row.retailer or '—')} | "
        f"{_fmt_pence(row.buy_price_pence)} | {_fmt_pence(row.net_profit_pence)} | {roi} | {row.verdict} |"
    )


def render_index(rows: list[DealRow]) -> str:
    """A single self-regenerating hub note linking to every current deal
    note -- fully overwritten each run, unlike the per-deal notes, since
    it's machine-owned: there's nowhere here for a user comment to survive."""
    frontmatter = yaml.safe_dump(
        {"tags": ["brightside-deal-index"], "deal_count": len(rows),
         "updated_at": datetime.now(timezone.utc).isoformat()},
        sort_keys=False,
    ).strip()
    header = (
        "| Deal | Title | Retailer | Buy | Profit | ROI | Verdict |\n"
        "|---|---|---|---|---|---|---|"
    )
    table = "\n".join([header] + [_index_row(r) for r in rows])
    return (
        f"---\n{frontmatter}\n---\n"
        f"# Confirmed Deals\n\n"
        f"{len(rows)} deals, newest first. Auto-generated by `app/obsidian_export.py` -- "
        f"edits to this note won't survive the next export; use a deal's own note for that.\n\n"
        f"{table}\n"
    )


def write_index(rows: list[DealRow], vault_dir: Path) -> Path:
    path = vault_dir / _INDEX_FILENAME
    path.write_text(render_index(rows), encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", default=os.environ.get("OBSIDIAN_VAULT_PATH"),
                         help="Path to your Obsidian vault (or set OBSIDIAN_VAULT_PATH)")
    parser.add_argument("--subdir", default=_DEFAULT_SUBDIR,
                         help=f"Folder within the vault to write notes into (default: {_DEFAULT_SUBDIR!r})")
    parser.add_argument("--limit", type=int, default=300, help="Max deals to export, newest first")
    args = parser.parse_args()

    if not args.vault:
        parser.error("--vault or OBSIDIAN_VAULT_PATH is required")

    vault_dir = Path(args.vault) / args.subdir

    db = SessionLocal()
    try:
        rows = dashboard.get_confirmed_deals(db, limit=args.limit)
    finally:
        db.close()
    rows = dedupe_by_filename(rows)

    created, updated = export_deals(rows, vault_dir)
    write_index(rows, vault_dir)
    print(f"Exported {len(rows)} deals to {vault_dir} ({created} created, {updated} updated, index refreshed).")


if __name__ == "__main__":
    main()
