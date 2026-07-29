"""Tests app.obsidian_export against plain DealRow objects -- no DB needed,
since export_deals only ever touches the filesystem."""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.dashboard import DealRow
from app.obsidian_export import (
    _note_filename,
    dedupe_by_filename,
    export_deals,
    render_index,
    render_note,
    write_index,
)


def _row(**overrides) -> DealRow:
    defaults = dict(
        title="Widget Pro 4000",
        retailer="Screwfix",
        retailer_url="https://screwfix.com/widget",
        asin="B0TESTASIN1",
        match_confidence="high",
        buy_price_pence=1299,
        sell_price_pence=2499,
        net_profit_pence=850,
        roi=0.654,
        est_monthly_sales=120.0,
        verdict="PASS",
        flags=[],
        ts=datetime(2026, 7, 29, 10, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return DealRow(**defaults)


def test_note_filename_uses_asin_when_present():
    assert _note_filename(_row(asin="B0ABC123")) == "B0ABC123.md"


def test_note_filename_slugifies_title_when_no_asin():
    row = _row(asin=None, title="Weird Title! With $ymbols")
    assert _note_filename(row) == "weird-title-with-ymbols.md"


def test_render_note_includes_frontmatter_fields():
    text = render_note(_row(), "\n## Notes\n\n")
    assert text.startswith("---\n")
    assert "asin: B0TESTASIN1" in text
    assert "buy_price: 12.99" in text
    assert "roi_pct: 65.4" in text
    assert "## Notes" in text


def test_export_creates_new_notes(tmp_path: Path):
    created, updated = export_deals([_row()], tmp_path)
    assert (created, updated) == (1, 0)
    note = (tmp_path / "B0TESTASIN1.md").read_text(encoding="utf-8")
    assert "verdict: PASS" in note
    assert note.endswith("## Notes\n\n")


def test_export_preserves_user_notes_on_rerun(tmp_path: Path):
    export_deals([_row()], tmp_path)
    note_path = tmp_path / "B0TESTASIN1.md"

    contents = note_path.read_text(encoding="utf-8")
    contents_with_user_note = contents + "Bought two from the local store, sold fast.\n"
    note_path.write_text(contents_with_user_note, encoding="utf-8")

    created, updated = export_deals([_row(roi=0.9, verdict="PASS_WITH_FLAGS")], tmp_path)
    assert (created, updated) == (0, 1)

    updated_text = note_path.read_text(encoding="utf-8")
    assert "roi_pct: 90.0" in updated_text
    assert "verdict: PASS_WITH_FLAGS" in updated_text
    assert "Bought two from the local store, sold fast." in updated_text


def test_export_reports_created_vs_updated_counts(tmp_path: Path):
    export_deals([_row(asin="B0FIRST0001")], tmp_path)
    created, updated = export_deals(
        [_row(asin="B0FIRST0001"), _row(asin="B0SECOND002", title="Second Widget")], tmp_path
    )
    assert (created, updated) == (1, 1)


def test_dedupe_by_filename_keeps_newest_row_when_two_deals_share_an_asin():
    newest = _row(asin="B0SHARED01", roi=0.9, retailer="Screwfix")
    older = _row(asin="B0SHARED01", roi=0.2, retailer="B&Q")
    deduped = dedupe_by_filename([newest, older])
    assert deduped == [newest]


def test_dedupe_by_filename_leaves_distinct_asins_untouched():
    a = _row(asin="B0ONE")
    b = _row(asin="B0TWO", title="Second Widget")
    assert dedupe_by_filename([a, b]) == [a, b]


def test_render_index_links_to_each_deal_note():
    text = render_index([_row(asin="B0ONE"), _row(asin="B0TWO", title="Second Widget")])
    assert "[[B0ONE]]" in text
    assert "[[B0TWO]]" in text
    assert "deal_count: 2" in text


def test_render_index_strips_pipe_characters_that_would_break_the_table():
    text = render_index([_row(title="Weird | Title", retailer="A | B Retail")])
    assert "Weird - Title" in text
    assert "A - B Retail" in text
    # exactly 7 columns (8 pipes) on the data row -- a stray "|" would add one
    data_row = [line for line in text.splitlines() if line.startswith("| [[")][0]
    assert data_row.count("|") == 8


def test_write_index_overwrites_on_rerun(tmp_path: Path):
    write_index([_row(asin="B0ONE")], tmp_path)
    path = write_index([_row(asin="B0ONE"), _row(asin="B0TWO", title="Second Widget")], tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "[[B0ONE]]" in text
    assert "[[B0TWO]]" in text
    assert "deal_count: 2" in text
