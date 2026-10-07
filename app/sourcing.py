"""ASIN -> where to buy it. Evidence is gathered cheapest first, and the web
is only searched for what the offline sources can't answer:

1. your scans      -- a supplier catalogue already scanned carries this exact
                      barcode/ASIN (supplier_scan_results), with its price
2. wholesaler board -- the 8 Sep board named suppliers for this ASIN or brand
3. supplier list   -- the brand or supplier appears in the vault's Supplier
                      Master List, which also gives your status with them
4. no-route signals -- Keepa shows an Amazon-only brand (seller in CN/HK,
                      one seller holding the buy box, brand selling direct)
5. web             -- Google UK via ScraperAPI for the brand's distributor,
                      trade page and anyone listing the barcode

Pure functions apart from scan_hits (DB) and the injected search callable,
so everything is testable offline.
"""
import re
from dataclasses import dataclass, field
from typing import Callable

from sqlalchemy import or_
from sqlalchemy.orm import Session

from . import models

# Marketplaces, big retailers and noise: they sell the product, they won't
# supply it.
_NOT_SUPPLIERS = (
    "amazon.", "ebay.", "argos.", "boots.", "superdrug.", "tesco.", "sainsburys.", "asda.", "morrisons.",
    "wilko.", "diy.com", "screwfix.", "toolstation.", "wickes.", "homebase.", "currys.", "johnlewis.",
    "very.co.uk", "onbuy.", "etsy.", "aliexpress.", "temu.", "walmart.", "facebook.", "youtube.", "instagram.",
    "tiktok.", "reddit.", "pinterest.", "wikipedia.", "trustpilot.", "lookfantastic.", "hollandandbarrett.",
    "google.", "idealo.", "pricespy.", "pricerunner.", "kelkoo.", "which.co.uk", "mumsnet.", "x.com", "twitter.",
    "linkedin.",
)
_HOST_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")
_DISTRIBUTOR_RE = re.compile(r"\b(distributor|distributors|distribution|distributed by|sole agent|uk agent)\b", re.I)
_WHOLESALE_RE = re.compile(
    r"\b(wholesale|wholesaler|wholesalers|trade account|trade only|trade customers|trade price|trade enquir\w*|"
    r"b2b|cash\s*(&|and)\s*carry|become a stockist|stockist enquir\w*|reseller)\b", re.I)
_STOCKIST_RE = re.compile(r"\b(where to buy|stockists?|find a (retailer|stockist)|trade)\b", re.I)
_NAMED_DISTRIBUTOR_RE = re.compile(
    r"\b(?:UK\s+)?distributor[s]?(?:\s+is|\s*,|\s*:)?\s+([A-Z][A-Za-z0-9&'.\-]+(?:\s+[A-Z][A-Za-z0-9&'.\-]+){0,3})")

SUPPLIER_DISPLAY = {   # supplier_scan_results keys -> the names used everywhere else
    "ancientwisdom": "Ancient Wisdom", "novanex": "Novanex", "pharmazon": "Pharmazon Global",
    "cosmetics": "Cherry Cosmetics", "honeypot": "Honeypot", "vp": "Global Pharma Distributor (Valupak)",
}


@dataclass
class Lead:
    name: str
    source: str              # "your scans" | "wholesaler board" | "supplier list" | "web"
    kind: str                # stocks_it | known_supplier | official_site | distributor | wholesaler
    url: str | None = None
    evidence: str = ""
    buy_price_pence: int | None = None
    status: str | None = None   # your Supplier Master List section for them


@dataclass
class SourcingReport:
    asin: str
    title: str | None
    brand: str | None
    eans: list[str]
    leads: list[Lead] = field(default_factory=list)
    no_route_signals: list[str] = field(default_factory=list)
    board_note: str | None = None
    verdict: str = ""


def norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


# --- 1. your scans ---

def scan_hits(db: Session, asin: str, eans: list[str]) -> list[Lead]:
    conds = [models.SupplierScanResult.asin == asin]
    if eans:
        conds.append(models.SupplierScanResult.ean.in_(eans))
    rows = db.query(models.SupplierScanResult).filter(or_(*conds)).all()
    return [
        Lead(name=SUPPLIER_DISPLAY.get(r.supplier, r.supplier), source="your scans", kind="stocks_it",
             evidence=f"in their catalogue as {r.name!r} (last seen {r.last_seen:%d %b %Y})" if r.last_seen
             else f"in their catalogue as {r.name!r}",
             buy_price_pence=r.buy_price_pence)
        for r in rows
    ]


# --- 2. wholesaler board ---

def board_hits(board: dict, asin: str, brand: str | None) -> tuple[list[Lead], str | None]:
    """(leads, the board's no-supplier note for this ASIN if it had one).
    Exact ASIN first; otherwise any board product whose name starts with
    the brand, since wholesalers carry brands, not single SKUs."""
    rows = board.get("asins") or {}
    row = rows.get(asin)
    if row:
        leads = [Lead(s["name"], "wholesaler board", "known_supplier", s.get("url"),
                      f"named for this ASIN on the 8 Sep board (prices: {s.get('price_access') or '?'})")
                 for s in row.get("suppliers") or []]
        return leads, row.get("no_supplier_reason")
    b = norm(brand)
    if len(b) < 3:
        return [], None
    seen: dict[str, Lead] = {}
    for other_asin, other in rows.items():
        if not norm(other.get("product"))[: len(b)] == b:
            continue
        for s in other.get("suppliers") or []:
            seen.setdefault(s["name"], Lead(
                s["name"], "wholesaler board", "known_supplier", s.get("url"),
                f"named for other {brand} products on the 8 Sep board (e.g. {other_asin}; "
                f"prices: {s.get('price_access') or '?'})"))
    return list(seen.values()), None


# --- 3. supplier master list ---

def _sections(master_md: str) -> list[tuple[str, str]]:
    """(section heading, line) for every line of the Supplier Master List."""
    out, heading = [], ""
    for line in master_md.splitlines():
        if line.startswith("## "):
            heading = line[3:].strip()
        out.append((heading, line))
    return out


def status_for(master_md: str, *names: str) -> str | None:
    """The Master List section a supplier is filed under ("Declined",
    "Applied or emailed -- waiting on a reply"...), matched on any of
    `names` (a supplier name, a website host). A host also tries its first
    label, so qogita.com finds the "Qogita" row."""
    variants = []
    for n in names:
        variants.append(n)
        if "." in (n or "") and " " not in n:
            variants.append(n.split(".")[0])
    patterns = [re.compile(rf"(?<![a-z0-9]){re.escape(v.lower().strip())}(?![a-z0-9])")
                for v in variants if len(norm(v)) >= 4]
    for heading, line in _sections(master_md):
        if heading and any(p.search(line.lower()) for p in patterns):
            return heading
    return None


def master_list_leads(master_md: str, brand: str | None) -> list[Lead]:
    """Table rows that mention the brand -- usually the supplier you
    contacted about it, with what happened."""
    if len(norm(brand)) < 4:
        return []
    # Whole words only: "Anua" must not match "manually".
    pattern = re.compile(rf"(?<![a-z0-9]){re.escape(brand.lower())}(?![a-z0-9])")
    leads = []
    for heading, line in _sections(master_md):
        if not line.startswith("|") or not pattern.search(line.lower()):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        name = re.sub(r"\*|\[\[.*?\||\]\]|\[\[", "", cells[0]).strip()
        if not name or set(name) <= set("-: "):
            continue
        leads.append(Lead(name, "supplier list", "known_supplier",
                          evidence=" | ".join(cells[1:])[:220], status=heading))
    return leads


# --- 4. no-route signals ---

def no_route_signals(product: dict, main_seller_country: str | None,
                     brand_matches_seller: bool | None) -> list[str]:
    signals = []
    if main_seller_country in ("CN", "HK"):
        signals.append(f"main buy-box seller is based in {main_seller_country}")
    if any(o.get("shipsFromChina") for o in product.get("offers") or []):
        signals.append("an offer ships from China")
    bb = (product.get("stats") or {}).get("buyBoxStats") or {}
    if bb:
        top = max(bb.values(), key=lambda v: v.get("percentageWon") or 0)
        if (top.get("percentageWon") or 0) >= 90:
            signals.append(f"one seller won {top['percentageWon']:.0f}% of the buy box over 90 days")
    if brand_matches_seller:
        signals.append("the brand itself holds the buy box")
    return signals


# --- 5. web ---

def _host_and_url(displayed_link: str) -> tuple[str, str]:
    """'https://peelaway.co.uk › where-to-buy' -> ('peelaway.co.uk', 'https://peelaway.co.uk/where-to-buy')."""
    parts = [p.strip() for p in (displayed_link or "").split("›")]
    host = re.sub(r"^https?://(www\.)?", "", parts[0]).rstrip("/")
    path = "/".join(p for p in parts[1:] if p and "..." not in p)
    return host, f"https://{host}" + (f"/{path}" if path else "")


def classify_result(result: dict, brand: str | None) -> Lead | None:
    host, url = _host_and_url(result.get("displayed_link") or "")
    if not _HOST_RE.match(host) or any(bad in host for bad in _NOT_SUPPLIERS):
        return None
    text = f"{result.get('title') or ''} {result.get('snippet') or ''} {url}"
    named = _NAMED_DISTRIBUTOR_RE.search(result.get("snippet") or "")
    evidence = (result.get("snippet") or result.get("title") or "")[:200]
    b = norm(brand)
    if len(b) >= 4 and b in norm(host):
        if named:
            evidence = f"brand site names its UK distributor: {named.group(1)}. {evidence}"
        elif not _STOCKIST_RE.search(text) and not _WHOLESALE_RE.search(text):
            evidence = f"brand's own site (look for a trade or stockist page). {evidence}"
        return Lead(host, "web", "official_site", url, evidence)
    if _DISTRIBUTOR_RE.search(text):
        return Lead(host, "web", "distributor", url, evidence)
    if _WHOLESALE_RE.search(text):
        return Lead(host, "web", "wholesaler", url, evidence)
    return None


def product_line(title: str | None, brand: str | None) -> str | None:
    """The title's leading word when it isn't the brand: Keepa files Peelaway
    under its maker, Barrettine, but distributors list it as Peelaway."""
    first = re.split(r"[\s\-|,]+", (title or "").strip())[0] if title else ""
    return first if len(norm(first)) >= 4 and norm(first) not in norm(brand) and norm(brand) not in norm(first) else None


def search_queries(brand: str | None, eans: list[str], line: str | None = None) -> list[str]:
    queries = []
    if brand:
        queries += [f'"{brand}" UK distributor', f'"{brand}" wholesale UK trade account']
    if line:
        queries.append(f'"{line}" UK distributor')
    if eans:
        queries.append(f'"{eans[0]}" wholesale')
    return queries


_MAX_OFFICIAL = 2   # one brand can own several sites (Barrettine has four)


def web_leads(search: Callable[[str], list[dict]], brand: str | None, eans: list[str],
              title: str | None = None) -> list[Lead]:
    line = product_line(title, brand)
    seen: dict[str, Lead] = {}
    for q in search_queries(brand, eans, line):
        for r in search(q):
            lead = classify_result(r, brand) or (classify_result(r, line) if line else None)
            if lead and lead.name not in seen:
                lead.evidence = f"[{q}] {lead.evidence}"
                seen[lead.name] = lead
    order = {"official_site": 0, "distributor": 1, "wholesaler": 2}
    leads = sorted(seen.values(), key=lambda l: order.get(l.kind, 9))
    official = [l for l in leads if l.kind == "official_site"]
    return official[:_MAX_OFFICIAL] + [l for l in leads if l.kind != "official_site"]


# --- verdict ---

def verdict(report: SourcingReport) -> str:
    kinds = {l.kind for l in report.leads}
    if "stocks_it" in kinds:
        return "STOCKED: a supplier you've already scanned carries it"
    if "known_supplier" in kinds:
        return "KNOWN SUPPLIERS: in your board or supplier list"
    web = [l for l in report.leads if l.source == "web"]
    if any(l.kind in ("official_site", "distributor") for l in web):
        return "LEADS ONLINE: brand site or distributor found, verify and contact"
    if len(report.no_route_signals) >= 2 or (report.no_route_signals and not web):
        return "LIKELY NO UK ROUTE: looks like an Amazon-only or brand-controlled product"
    if web:
        return "WEAK LEADS: only general wholesalers found online"
    return "NO ROUTE FOUND"


def render_markdown(report: SourcingReport, assessment_lines: list[str]) -> str:
    lines = [
        f"# Sourcing: {report.asin}",
        "",
        f"**{report.title or '?'}**  ",
        f"Brand: {report.brand or '?'} · Barcode: {', '.join(report.eans) or 'none on Keepa'} · "
        f"[Amazon](https://www.amazon.co.uk/dp/{report.asin})",
        "",
        f"## Verdict: {report.verdict}",
        "",
        *assessment_lines,
    ]
    if report.board_note:
        lines += ["", f"8 Sep board said: *{report.board_note}*"]
    if report.no_route_signals:
        lines += ["", "**No-route signals:** " + "; ".join(report.no_route_signals)]
    lines += ["", "## Leads", ""]
    if not report.leads:
        lines.append("None found.")
    for l in report.leads:
        bits = [f"**{l.name}**", f"({l.source}, {l.kind.replace('_', ' ')})"]
        if l.buy_price_pence is not None:
            bits.append(f"buy price £{l.buy_price_pence / 100:.2f}")
        if l.status:
            bits.append(f"your status: {l.status}")
        if l.url:
            bits.append(l.url)
        lines.append("- " + " · ".join(bits))
        if l.evidence:
            lines.append(f"  - {l.evidence}")
    return "\n".join(lines) + "\n"
