from app import models, sourcing
from app.sourcing import Lead, SourcingReport

# Real google.co.uk results via ScraperAPI, 2026-10-01.
PEELAWAY_SITE = {
    "title": "www.peelaway.co.uk",
    "snippet": "For samples and to find the nearest Peel Away suppliers, please contact our UK distributor Barrettine Products Ltd.",
    "displayed_link": "https://peelaway.co.uk",
}
PEELAWAY_WHERE = {
    "title": "Where to buy",
    "snippet": "Where to buy · Bromborough Paints · Dulux · Huddersfield Decorators",
    "displayed_link": "https://peelaway.co.uk › where-to-buy",
}

BOARD = {"asins": {
    "B00BEF43QC": {"product": "Osmo Polyx-Oil Effect 3044C", "suppliers": [
        {"name": "Osmo UK Ltd", "url": "https://osmouk.com", "price_access": "quote"},
        {"name": "Hart Wholesale", "url": "https://blum.org.uk", "price_access": "public"}],
        "no_supplier_reason": None},
    "B0DF2LSQKH": {"product": "TidyTrove Storage Boxes", "suppliers": [],
                   "no_supplier_reason": "Amazon-native private label"},
}}

MASTER = """# Supplier master list

## Declined

| Supplier | What for | Reason given |
|---|---|---|
| Osmo UK Ltd | Wood finish | Too many online sellers already. |

## Applied or emailed — waiting on a reply

| Supplier | What for | Sent | Status |
|---|---|---|---|
| Barrettine (brand) | Peelaway paint stripper | 26 Sep | No reply yet. |
"""


def test_host_and_url_from_displayed_link():
    assert sourcing._host_and_url("https://peelaway.co.uk › where-to-buy") == (
        "peelaway.co.uk", "https://peelaway.co.uk/where-to-buy")
    assert sourcing._host_and_url("https://www.barrettinepro.co.uk")[0] == "barrettinepro.co.uk"


def test_brand_site_naming_its_distributor():
    lead = sourcing.classify_result(PEELAWAY_SITE, "Peelaway")
    assert lead.kind == "official_site"
    assert "Barrettine Products Ltd" in lead.evidence


def test_marketplaces_are_not_suppliers():
    assert sourcing.classify_result(
        {"title": "Peelaway 7", "snippet": "wholesale", "displayed_link": "https://www.amazon.co.uk › dp"}, "Peelaway") is None


def test_wholesaler_and_plain_retailer():
    wholesale = {"title": "Trade paint supplies", "snippet": "Open a trade account for wholesale prices",
                 "displayed_link": "https://tradepaints.co.uk"}
    retail = {"title": "Peelaway 7 750g", "snippet": "Next day delivery", "displayed_link": "https://somepaintshop.co.uk"}
    assert sourcing.classify_result(wholesale, "Peelaway").kind == "wholesaler"
    assert sourcing.classify_result(retail, "Peelaway") is None


def test_board_exact_asin_and_brand_fallback():
    leads, note = sourcing.board_hits(BOARD, "B00BEF43QC", "Osmo")
    assert [l.name for l in leads] == ["Osmo UK Ltd", "Hart Wholesale"] and note is None
    leads, _ = sourcing.board_hits(BOARD, "B0NEWOSMO1", "OSMO")
    assert {l.name for l in leads} == {"Osmo UK Ltd", "Hart Wholesale"}
    assert "other OSMO products" in leads[0].evidence
    leads, note = sourcing.board_hits(BOARD, "B0DF2LSQKH", "TidyTrove")
    assert leads == [] and note == "Amazon-native private label"


def test_supplier_list_status_and_brand_rows():
    assert sourcing.status_for(MASTER, "Osmo UK Ltd") == "Declined"
    assert sourcing.status_for(MASTER, "", "barrettinepro.co.uk") is None   # host too different
    assert sourcing.status_for(MASTER, "Barrettine") == "Applied or emailed — waiting on a reply"
    leads = sourcing.master_list_leads(MASTER, "Peelaway")
    assert [(l.name, l.status) for l in leads] == [("Barrettine (brand)", "Applied or emailed — waiting on a reply")]


def test_no_route_signals():
    product = {"offers": [{"shipsFromChina": False}],
               "stats": {"buyBoxStats": {"A": {"percentageWon": 99.1}, "B": {"percentageWon": 0.9}}}}
    signals = sourcing.no_route_signals(product, "HK", True)
    assert signals == ["main buy-box seller is based in HK", "one seller won 99% of the buy box over 90 days",
                       "the brand itself holds the buy box"]
    assert sourcing.no_route_signals({"stats": {}}, "GB", False) == []


def test_web_leads_dedupe_and_order():
    calls = []

    def search(q):
        calls.append(q)
        return [PEELAWAY_WHERE, PEELAWAY_SITE,
                {"title": "Distributor of decorating products", "snippet": "UK distributor",
                 "displayed_link": "https://decorsupply.co.uk"}]

    leads = sourcing.web_leads(search, "Peelaway", ["5012345678900"])
    assert calls == ['"Peelaway" UK distributor', '"Peelaway" wholesale UK trade account', '"5012345678900" wholesale']
    assert [l.name for l in leads] == ["peelaway.co.uk", "decorsupply.co.uk"]


def test_verdict_prefers_the_strongest_evidence():
    r = SourcingReport("B0X", "t", "b", [])
    assert sourcing.verdict(r) == "NO ROUTE FOUND"
    r.no_route_signals = ["main buy-box seller is based in HK"]
    assert sourcing.verdict(r).startswith("LIKELY NO UK ROUTE")
    r.leads = [Lead("x.co.uk", "web", "official_site")]
    assert sourcing.verdict(r).startswith("LEADS ONLINE")
    r.leads.append(Lead("Hart", "wholesaler board", "known_supplier"))
    assert sourcing.verdict(r).startswith("KNOWN SUPPLIERS")
    r.leads.append(Lead("Novanex", "your scans", "stocks_it"))
    assert sourcing.verdict(r).startswith("STOCKED")


def test_scan_hits_by_barcode_or_asin(db_session):
    db_session.add_all([
        models.SupplierScanResult(supplier="novanex", ean="5012345678900", asin=None, name="Peelaway 7", stage=1,
                                  buy_price_pence=1050),
        models.SupplierScanResult(supplier="honeypot", ean="999", asin="B0SOURCE01", name="Other pack", stage=2),
        models.SupplierScanResult(supplier="vp", ean="111", asin="B0ELSE0001", name="Unrelated", stage=2),
    ])
    db_session.commit()
    leads = sourcing.scan_hits(db_session, "B0SOURCE01", ["5012345678900"])
    assert {(l.name, l.buy_price_pence) for l in leads} == {("Novanex", 1050), ("Honeypot", None)}


def test_product_line_when_brand_is_the_maker():
    assert sourcing.product_line("Peelaway 7 - Paint and Varnish Remover", "Barrettine") == "Peelaway"
    assert sourcing.product_line("Osmo Polyx-Oil 3032", "OSMO") is None


def test_non_domain_display_links_are_dropped():
    assert sourcing.classify_result({"title": "Barrettine Industrial", "snippet": "distributors",
                                     "displayed_link": "220+ followers"}, "Barrettine") is None


def test_official_sites_capped():
    sites = [{"title": f"Barrettine {i}", "snippet": "", "displayed_link": f"https://barrettine{i}.co.uk"} for i in range(4)]
    leads = sourcing.web_leads(lambda q: sites, "Barrettine", [])
    assert len(leads) == 2


def test_brand_match_is_whole_word():
    md = "## Blocked\n\n| Supplier | Product | Blocker |\n|---|---|---|\n| Granada Batteries | Rayovac | bypassed manually |\n"
    assert sourcing.master_list_leads(md, "ANUA") == []


def test_status_from_a_website_host():
    md = "## Declined\n\n| Supplier | What for | Reason |\n|---|---|---|\n| Qogita | Marketplace | Needs a UK VAT number. |\n"
    assert sourcing.status_for(md, "qogita.com") == "Declined"


def test_status_host_stem_is_whole_word():
    md = "## Blocked\n\n| Supplier | Product | Blocker |\n|---|---|---|\n| Granada Batteries | Rayovac | bypassed manually |\n"
    assert sourcing.status_for(md, "anua.com") is None
