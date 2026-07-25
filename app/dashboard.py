"""Read-only HTML dashboard listing confirmed good deals (Score.verdict in
PASS/PASS_WITH_FLAGS) -- the "spreadsheet" view of everything the pipeline
has actually financially vetted, not just whatever got pinged to Discord
(cooldown/ping failures are excluded there but not here -- a verdict is
"confirmed good" the moment scoring says so, independent of notification
plumbing). No auth: matches GET /status/summary's existing precedent --
read-only endpoints in this app aren't gated behind the shared secret, that
protects writes only (see auth.py's docstring). Deal/product titles are
untrusted retailer-sourced text, so every dynamic value is html.escape()'d
before being written into the page -- this is the only place in the app
that renders scraped text as HTML rather than JSON."""
import html
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models

_GOOD_VERDICTS = ("PASS", "PASS_WITH_FLAGS")


@dataclass
class DealRow:
    title: str
    retailer: str | None
    retailer_url: str | None
    asin: str | None
    match_confidence: str | None
    buy_price_pence: int
    sell_price_pence: int | None
    net_profit_pence: int | None
    roi: float | None
    est_monthly_sales: float | None
    verdict: str
    flags: list[str]
    ts: datetime | None


def get_confirmed_deals(db: Session, limit: int = 300) -> list[DealRow]:
    """Latest Score per deal (a deal can be re-scored across price changes
    -- see pipeline.py), filtered to genuinely good verdicts, newest first."""
    latest_score_ids = db.query(func.max(models.Score.id)).group_by(models.Score.deal_id)
    rows = (
        db.query(models.Score, models.Deal, models.Product)
        .join(models.Deal, models.Score.deal_id == models.Deal.id)
        .outerjoin(models.Product, models.Deal.product_id == models.Product.id)
        .filter(models.Score.id.in_(latest_score_ids.subquery().select()))
        .filter(models.Score.verdict.in_(_GOOD_VERDICTS))
        .order_by(models.Score.ts.desc())
        .limit(limit)
        .all()
    )
    out = []
    for score, deal, product in rows:
        # A scan's retailer_url is the synthetic "scan:<ean>:<uuid>" dedup
        # key (see app/scan.py), not a real link -- same caveat pipeline.py
        # already works around for the Discord embed.
        retailer_url = None if deal.source == "scan" else (deal.retailer_url or deal.url)
        out.append(DealRow(
            title=(product.title if product and product.title else deal.title),
            retailer=deal.retailer,
            retailer_url=retailer_url,
            asin=product.asin if product else None,
            match_confidence=product.confidence if product else None,
            buy_price_pence=deal.buy_price,
            sell_price_pence=score.sell_price,
            net_profit_pence=score.net_profit,
            roi=score.roi,
            est_monthly_sales=score.est_monthly_sales,
            verdict=score.verdict,
            flags=score.flags_json or [],
            ts=score.ts,
        ))
    return out


def _esc(value) -> str:
    return html.escape(str(value)) if value is not None else ""


def _fmt_pence(pence: int | None) -> str:
    return f"£{pence / 100:,.2f}" if pence is not None else "—"


def _fmt_pct(roi: float | None) -> str:
    return f"{roi * 100:.0f}%" if roi is not None else "—"


def _fmt_date(ts: datetime | None) -> str:
    return ts.strftime("%d %b %Y, %H:%M") if ts else "—"


def _row_html(row: DealRow) -> str:
    amazon_url = f"https://www.amazon.co.uk/dp/{row.asin}" if row.asin else None
    title_cell = f'<a href="{_esc(amazon_url)}" target="_blank" rel="noopener">{_esc(row.title)}</a>' if amazon_url else _esc(row.title)
    retailer_cell = (
        f'<a href="{_esc(row.retailer_url)}" target="_blank" rel="noopener">{_esc(row.retailer or "link")}</a>'
        if row.retailer_url else _esc(row.retailer or "—")
    )
    verdict_class = "badge-pass" if row.verdict == "PASS" else "badge-flags"
    profit_class = "profit-pos" if (row.net_profit_pence or 0) > 0 else ""
    flags_cell = "".join(f'<span class="chip">{_esc(f)}</span>' for f in row.flags) or "—"
    confidence_cell = f'<span class="chip chip-{_esc(row.match_confidence)}">{_esc(row.match_confidence or "—")}</span>'

    return f"""<tr>
      <td class="col-title">{title_cell}</td>
      <td>{retailer_cell}</td>
      <td class="num">{_fmt_pence(row.buy_price_pence)}</td>
      <td class="num">{_fmt_pence(row.sell_price_pence)}</td>
      <td class="num {profit_class}">{_fmt_pence(row.net_profit_pence)}</td>
      <td class="num {profit_class}">{_fmt_pct(row.roi)}</td>
      <td class="num">{_esc(row.est_monthly_sales) if row.est_monthly_sales is not None else "—"}</td>
      <td>{confidence_cell}</td>
      <td><span class="badge {verdict_class}">{_esc(row.verdict.replace("_", " "))}</span></td>
      <td class="col-flags">{flags_cell}</td>
      <td class="col-date">{_fmt_date(row.ts)}</td>
    </tr>"""


_COLUMNS = [
    ("Product", False), ("Retailer", False), ("Buy", True), ("Sell", True),
    ("Profit", True), ("ROI", True), ("Est/mo", True), ("Match", False),
    ("Verdict", False), ("Flags", False), ("Scored", False),
]


def render_page(rows: list[DealRow]) -> str:
    body_rows = "\n".join(_row_html(r) for r in rows) or (
        '<tr><td colspan="11" class="empty">No confirmed deals yet — check back after the next poll.</td></tr>'
    )
    headers = "\n".join(
        f'<th class="{"num" if numeric else ""}" data-col="{i}">{_esc(name)}</th>'
        for i, (name, numeric) in enumerate(_COLUMNS)
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Brightside — Confirmed Deals</title>
<style>
  :root {{
    --bg: #f7f8fa; --panel: #ffffff; --border: #e3e6ea; --text: #1a1d21;
    --muted: #6b7280; --accent: #2563eb; --green: #15803d; --green-bg: #dcfce7;
    --amber: #92400e; --amber-bg: #fef3c7; --chip-bg: #eef1f5;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #0f1115; --panel: #171a21; --border: #2a2f3a; --text: #e6e8eb;
      --muted: #9aa3af; --accent: #60a5fa; --green: #4ade80; --green-bg: #14351f;
      --amber: #fbbf24; --amber-bg: #3a2f0f; --chip-bg: #232733;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    padding: 24px 16px 64px;
  }}
  .wrap {{ max-width: 1400px; margin: 0 auto; }}
  h1 {{ font-size: 22px; font-weight: 700; margin: 0 0 4px; }}
  .subtitle {{ color: var(--muted); font-size: 14px; margin: 0 0 20px; }}
  .panel {{
    background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
    overflow: auto; box-shadow: 0 1px 2px rgba(0,0,0,0.04);
  }}
  table {{ border-collapse: collapse; width: 100%; font-size: 13.5px; min-width: 980px; }}
  thead th {{
    position: sticky; top: 0; background: var(--panel); text-align: left;
    padding: 12px 14px; border-bottom: 2px solid var(--border); font-weight: 600;
    color: var(--muted); text-transform: uppercase; font-size: 11px; letter-spacing: 0.04em;
    cursor: pointer; user-select: none; white-space: nowrap;
  }}
  thead th:hover {{ color: var(--accent); }}
  thead th.num {{ text-align: right; }}
  tbody td {{ padding: 11px 14px; border-bottom: 1px solid var(--border); vertical-align: middle; }}
  tbody tr:hover {{ background: var(--chip-bg); }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  td.col-title {{ max-width: 340px; }}
  td.col-flags {{ max-width: 260px; }}
  td.col-date {{ color: var(--muted); white-space: nowrap; }}
  td.empty {{ text-align: center; padding: 48px; color: var(--muted); }}
  a {{ color: var(--accent); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .profit-pos {{ color: var(--green); font-weight: 600; }}
  .badge {{ display: inline-block; padding: 3px 10px; border-radius: 999px; font-size: 11.5px; font-weight: 600; }}
  .badge-pass {{ background: var(--green-bg); color: var(--green); }}
  .badge-flags {{ background: var(--amber-bg); color: var(--amber); }}
  .chip {{
    display: inline-block; background: var(--chip-bg); color: var(--muted);
    border-radius: 6px; padding: 2px 8px; font-size: 11px; margin: 1px 3px 1px 0;
  }}
  .chip-high {{ color: var(--green); }}
  .chip-low {{ color: var(--amber); }}
  footer {{ text-align: center; color: var(--muted); font-size: 12px; margin-top: 16px; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>Confirmed Deals</h1>
  <p class="subtitle">{len(rows)} product{"s" if len(rows) != 1 else ""} that passed profitability scoring, newest first. Click a column header to sort.</p>
  <div class="panel">
    <table id="deals">
      <thead><tr>{headers}</tr></thead>
      <tbody>
{body_rows}
      </tbody>
    </table>
  </div>
  <footer>Brightside FBA scanner</footer>
</div>
<script>
  (function () {{
    var table = document.getElementById("deals");
    var tbody = table.tBodies[0];
    var headers = table.tHead.rows[0].cells;
    var dir = {{}};
    for (var i = 0; i < headers.length; i++) {{
      headers[i].addEventListener("click", (function (idx) {{
        return function () {{
          var rows = Array.prototype.slice.call(tbody.rows);
          if (rows.length < 2) return;
          dir[idx] = !dir[idx];
          var numeric = headers[idx].classList.contains("num");
          rows.sort(function (a, b) {{
            var av = a.cells[idx].innerText.trim();
            var bv = b.cells[idx].innerText.trim();
            if (numeric) {{
              av = parseFloat(av.replace(/[^0-9.-]/g, "")) || 0;
              bv = parseFloat(bv.replace(/[^0-9.-]/g, "")) || 0;
              return dir[idx] ? av - bv : bv - av;
            }}
            return dir[idx] ? av.localeCompare(bv) : bv.localeCompare(av);
          }});
          rows.forEach(function (r) {{ tbody.appendChild(r); }});
        }};
      }})(i));
    }}
  }})();
</script>
</body>
</html>"""
