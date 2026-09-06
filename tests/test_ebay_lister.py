"""ebay_lister -- CSV validation, payload shapes, and the three-stage run
with its resume behaviour. eBay itself is stubbed at the ebay_client seam."""
import json

import pytest

from app import ebay_client, ebay_lister, models
from app.config import EbaySettings


def _settings(**overrides) -> EbaySettings:
    base = dict(
        env="sandbox", client_id="cid", client_secret="csecret", ru_name="ru",
        refresh_token="rt", marketplace_id="EBAY_GB", merchant_location_key="HOME",
        fulfillment_policy_id="F1", payment_policy_id="P1", return_policy_id="R1",
    )
    base.update(overrides)
    return EbaySettings(**base)


@pytest.fixture(autouse=True)
def _settings_everywhere(monkeypatch):
    monkeypatch.setattr(ebay_lister, "get_ebay_settings", _settings)
    monkeypatch.setattr(ebay_client, "get_ebay_settings", _settings)


def _raw(**overrides) -> dict:
    row = {
        "sku": "HAY-0001", "title": "", "make": "Ford", "model": "Fiesta",
        "year_from": "1995", "year_to": "2002", "haynes_number": "3397",
        "isbn": "9781859606667", "condition": "USED_GOOD", "price_gbp": "8.99",
        "quantity": "1", "category_id": "34248", "condition_description": "Light wear.",
        "image_urls": "https://example.com/a.jpg|https://example.com/b.jpg",
    }
    row.update(overrides)
    return row


# --- parsing / validation --------------------------------------------------

def test_parses_a_good_row():
    rows, problems = ebay_lister.parse_rows([_raw()])
    assert problems == []
    assert rows[0].sku == "HAY-0001"
    assert rows[0].price_pence == 899
    # eBay's Amount.value is a string, not a number.
    assert rows[0].price_string == "8.99"
    assert rows[0].image_urls == ["https://example.com/a.jpg", "https://example.com/b.jpg"]


def test_blank_title_is_built_from_make_model_years():
    rows, _ = ebay_lister.parse_rows([_raw(title="")])
    assert rows[0].title == "Haynes Manual Ford Fiesta 1995-2002 Service Repair Workshop"


def test_condition_accepts_friendly_wording():
    rows, problems = ebay_lister.parse_rows([_raw(condition="very good")])
    assert problems == []
    assert rows[0].condition == "USED_VERY_GOOD"


def test_unknown_condition_is_rejected_with_the_valid_list():
    rows, problems = ebay_lister.parse_rows([_raw(condition="mint-ish")])
    assert rows == []
    assert "USED_GOOD" in problems[0]


def test_price_accepts_a_pound_sign_and_rejects_nonsense():
    rows, _ = ebay_lister.parse_rows([_raw(price_gbp="£12.50")])
    assert rows[0].price_pence == 1250
    _, problems = ebay_lister.parse_rows([_raw(price_gbp="free")])
    assert "not a positive number" in problems[0]


def test_zero_price_is_rejected():
    _, problems = ebay_lister.parse_rows([_raw(price_gbp="0")])
    assert problems


def test_title_over_ebays_80_char_limit_is_caught_before_sending():
    rows, problems = ebay_lister.parse_rows([_raw(title="H" * 81)])
    assert rows == []
    assert "80" in problems[0]


def test_non_https_images_are_rejected():
    # eBay fetches images by URL -- a local path fails mid-batch otherwise.
    _, problems = ebay_lister.parse_rows([_raw(image_urls="C:/photos/a.jpg")])
    assert "https" in problems[0]


def test_duplicate_skus_are_rejected():
    rows, problems = ebay_lister.parse_rows([_raw(), _raw()])
    assert len(rows) == 1
    assert "duplicate" in problems[0]


def test_one_bad_row_does_not_stop_the_good_ones():
    rows, problems = ebay_lister.parse_rows(
        [_raw(sku="A"), _raw(sku="B", price_gbp="oops"), _raw(sku="C")])
    assert [r.sku for r in rows] == ["A", "C"]
    assert len(problems) == 1


def test_load_csv_reports_missing_required_columns(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("sku,title\nA,B\n", encoding="utf-8")
    rows, problems = ebay_lister.load_csv(path)
    assert rows == []
    assert "condition" in problems[0] and "price_gbp" in problems[0]


def test_load_csv_round_trips_the_shipped_template():
    rows, problems = ebay_lister.load_csv("haynes_manuals_template.csv")
    assert problems == []
    assert len(rows) == 2


# --- payload shapes --------------------------------------------------------

def test_inventory_item_payload_shape():
    row = ebay_lister.parse_rows([_raw()])[0][0]
    item = ebay_lister.build_inventory_item(row)

    # InventoryItemWithSkuLocale -- sku/locale/condition/product/
    # availability are all siblings. eBay silently treats a nested wrapper
    # key as an empty item (errorId 25002) rather than rejecting it, so this
    # flat shape is the one thing here worth locking down with a test.
    assert item["sku"] == "HAY-0001"
    assert item["condition"] == "USED_GOOD"
    assert item["availability"]["shipToLocationAvailability"]["quantity"] == 1
    # Aspect values are lists of strings even when single -- a bare string
    # is rejected by eBay.
    assert item["product"]["aspects"]["Brand"] == ["Haynes"]
    assert item["product"]["aspects"]["Make"] == ["Ford"]
    assert item["product"]["aspects"]["Year"] == ["1995-2002"]
    # ISBN is a list too, and lets eBay match its own catalogue entry.
    assert item["product"]["isbn"] == ["9781859606667"]
    assert item["product"]["mpn"] == "3397"


def test_condition_description_is_omitted_for_new_items():
    row = ebay_lister.parse_rows([_raw(condition="NEW")])[0][0]
    assert "conditionDescription" not in ebay_lister.build_inventory_item(row)


def test_offer_payload_shape():
    row = ebay_lister.parse_rows([_raw()])[0][0]
    offer = ebay_lister.build_offer(row)

    assert offer["format"] == "FIXED_PRICE"
    assert offer["marketplaceId"] == "EBAY_GB"
    assert offer["categoryId"] == "34248"
    assert offer["merchantLocationKey"] == "HOME"
    assert offer["pricingSummary"]["price"] == {"value": "8.99", "currency": "GBP"}
    assert offer["listingPolicies"]["fulfillmentPolicyId"] == "F1"
    assert offer["listingPolicies"]["bestOfferTerms"]["bestOfferEnabled"] is True


def test_offer_falls_back_to_the_default_category():
    row = ebay_lister.parse_rows([_raw(category_id="")])[0][0]
    assert ebay_lister.build_offer(row, category_id="99999")["categoryId"] == "99999"


# --- the run ---------------------------------------------------------------

def _stub_ebay(monkeypatch, *, offer_ok=True, publish_ok=True):
    monkeypatch.setattr(ebay_client, "bulk_create_or_replace_inventory_items",
                        lambda items: [ebay_client.BulkItemResult(
                            ok=True, status_code=200, sku=i["sku"]) for i in items])
    monkeypatch.setattr(ebay_client, "bulk_create_offers",
                        lambda offers: [ebay_client.BulkItemResult(
                            ok=offer_ok, status_code=200 if offer_ok else 400,
                            sku=o["sku"], offer_id=f"OF-{o['sku']}" if offer_ok else None,
                            errors=[] if offer_ok else ["offer rejected"]) for o in offers])
    monkeypatch.setattr(ebay_client, "bulk_publish_offers",
                        lambda ids: [ebay_client.BulkItemResult(
                            ok=publish_ok, status_code=200 if publish_ok else 400,
                            offer_id=i, listing_id=f"L-{i}" if publish_ok else None,
                            errors=[] if publish_ok else ["publish rejected"]) for i in ids])


def test_full_run_publishes_and_records(db_session, monkeypatch):
    _stub_ebay(monkeypatch)
    rows, _ = ebay_lister.parse_rows([_raw(sku="A"), _raw(sku="B")])
    result = ebay_lister.list_manuals(db_session, rows)

    assert sorted(result.published) == ["A", "B"]
    assert result.failed == []
    listing = db_session.get(models.EbayListing, "A")
    assert listing.status == "published"
    assert listing.offer_id == "OF-A"
    assert listing.listing_id == "L-OF-A"


def test_offer_failure_is_recorded_and_stops_that_sku(db_session, monkeypatch):
    _stub_ebay(monkeypatch, offer_ok=False)
    rows, _ = ebay_lister.parse_rows([_raw(sku="A")])
    result = ebay_lister.list_manuals(db_session, rows)

    assert result.failed == ["A"]
    listing = db_session.get(models.EbayListing, "A")
    assert listing.status == "failed"
    assert "offer" in listing.last_error and "offer rejected" in listing.last_error


def test_rerun_skips_already_published_skus(db_session, monkeypatch):
    _stub_ebay(monkeypatch)
    rows, _ = ebay_lister.parse_rows([_raw(sku="A")])
    ebay_lister.list_manuals(db_session, rows)

    # Second run must not re-create the offer -- bulk_create_offer is not
    # idempotent, so a duplicate call is an error from eBay, not a no-op.
    def explode(*args, **kwargs):
        raise AssertionError("should not have called eBay again")

    monkeypatch.setattr(ebay_client, "bulk_create_offers", explode)
    result = ebay_lister.list_manuals(db_session, rows)
    assert result.skipped == ["A"]
    assert result.published == []


def test_no_publish_stops_at_offer_created(db_session, monkeypatch):
    _stub_ebay(monkeypatch)
    monkeypatch.setattr(ebay_client, "bulk_publish_offers",
                        lambda ids: (_ for _ in ()).throw(AssertionError("should not publish")))
    rows, _ = ebay_lister.parse_rows([_raw(sku="A")])
    result = ebay_lister.list_manuals(db_session, rows, publish=False)

    assert result.published == ["A"]
    assert db_session.get(models.EbayListing, "A").status == "offer_created"


def test_run_refuses_to_publish_when_prerequisites_missing(db_session, monkeypatch):
    monkeypatch.setattr(ebay_client, "get_ebay_settings",
                        lambda: _settings(return_policy_id=""))
    rows, _ = ebay_lister.parse_rows([_raw(sku="A")])
    result = ebay_lister.list_manuals(db_session, rows)

    assert result.published == [] and result.failed == []
    assert "EBAY_RETURN_POLICY_ID" in result.problems[0]


def test_run_refuses_when_a_row_has_no_category(db_session, monkeypatch):
    _stub_ebay(monkeypatch)
    rows, _ = ebay_lister.parse_rows([_raw(sku="A", category_id="")])
    result = ebay_lister.list_manuals(db_session, rows)
    assert "categoryId" in result.problems[0]


def test_dry_run_sends_nothing(db_session, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("dry run must not call eBay")

    monkeypatch.setattr(ebay_client, "bulk_create_or_replace_inventory_items", explode)
    rows, _ = ebay_lister.parse_rows([_raw(sku="A", category_id="")])
    result = ebay_lister.list_manuals(db_session, rows, dry_run=True)

    assert result.published == [] and result.failed == []
    assert "dry run" in result.notes[0]
    assert result.problems == []
    # ...and writes nothing either: a dry run has no side effects at all.
    assert db_session.get(models.EbayListing, "A") is None


def test_dry_run_needs_no_database(monkeypatch):
    # The point of --dry-run is checking a 100-row sheet on a laptop with
    # no DATABASE_URL, so it must work with db=None.
    rows, _ = ebay_lister.parse_rows([_raw(sku="A")])
    result = ebay_lister.list_manuals(None, rows, dry_run=True)
    assert "dry run" in result.notes[0]


def test_real_run_without_a_db_is_a_clear_error():
    rows, _ = ebay_lister.parse_rows([_raw(sku="A")])
    with pytest.raises(ValueError, match="db is required"):
        ebay_lister.list_manuals(None, rows)


def test_publish_failure_resumes_at_publish_without_a_second_offer(db_session, monkeypatch):
    """The failure mode that matters most on a retry: a SKU whose offer was
    created but whose publish failed is left at 'failed' while still owning
    a live unpublished offer. Re-running must resume at stage 3 -- asking
    eBay to create a second offer for that SKU is an error, not a no-op."""
    _stub_ebay(monkeypatch, publish_ok=False)
    rows, _ = ebay_lister.parse_rows([_raw(sku="A")])
    ebay_lister.list_manuals(db_session, rows)

    listing = db_session.get(models.EbayListing, "A")
    assert listing.status == "failed" and listing.offer_id == "OF-A"

    # Second run: neither earlier stage may fire again.
    def explode(*args, **kwargs):
        raise AssertionError("must not re-create inventory or offers")

    monkeypatch.setattr(ebay_client, "bulk_create_or_replace_inventory_items", explode)
    monkeypatch.setattr(ebay_client, "bulk_create_offers", explode)
    monkeypatch.setattr(ebay_client, "bulk_publish_offers",
                        lambda ids: [ebay_client.BulkItemResult(
                            ok=True, status_code=200, offer_id=i, listing_id=f"L-{i}")
                            for i in ids])

    result = ebay_lister.list_manuals(db_session, rows)
    assert result.published == ["A"]
    listing = db_session.get(models.EbayListing, "A")
    assert listing.status == "published" and listing.listing_id == "L-OF-A"


def test_unknown_columns_are_ignored_and_never_reach_ebay(tmp_path):
    """The stock sheet carries internal columns eBay must never see --
    `packing_note` records which copies still need shrink-wrapping. That is
    a stock-room fact, not a listing field, and leaking it into the payload
    would advertise a defect that does not exist."""
    path = tmp_path / "stock.csv"
    path.write_text(
        "sku,condition,price_gbp,make,model,packing_note,internal_cost\n"
        "BB-001,USED_GOOD,9.99,Honda,CR-V,1 of 2 needs sealing,2.50\n",
        encoding="utf-8")

    rows, problems = ebay_lister.load_csv(path)
    assert problems == []

    payload = json.dumps([ebay_lister.build_inventory_item(rows[0]),
                          ebay_lister.build_offer(rows[0], category_id="1")])
    assert "seal" not in payload.lower()
    assert "2.50" not in payload
