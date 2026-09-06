"""ebay_cards -- CSV validation, AUCTION-format payload shapes, and the
three-stage run with its resume behaviour. Sibling to test_ebay_lister.py;
see app/ebay_cards.py's module docstring for why this is a separate module
rather than an extension of ebay_lister.py. eBay itself is stubbed at the
ebay_client seam, same as the manuals tests."""
import pytest

from app import ebay_cards, ebay_client, models
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
    monkeypatch.setattr(ebay_cards, "get_ebay_settings", _settings)
    monkeypatch.setattr(ebay_client, "get_ebay_settings", _settings)


def _raw(**overrides) -> dict:
    row = {
        "sku": "PC-001", "title": "Vaporeon EX 24/83 Generations Radiant Collection",
        "card_condition": "NM", "start_price_gbp": "15.00", "buy_it_now_price_gbp": "30.00",
        "listing_duration": "DAYS_7", "description": "Raw, ungraded.",
        "image_urls": "https://example.com/a.jpg|https://example.com/b.jpg",
    }
    row.update(overrides)
    return row


# --- parsing / validation --------------------------------------------------

def test_parses_a_good_row():
    rows, problems = ebay_cards.parse_rows([_raw()])
    assert problems == []
    assert rows[0].sku == "PC-001"
    assert rows[0].start_price_pence == 1500
    assert rows[0].buy_it_now_price_pence == 3000
    # eBay's Amount.value is a string, not a number.
    assert rows[0].start_price_string == "15.00"
    assert rows[0].buy_it_now_price_string == "30.00"


def test_unknown_card_condition_is_rejected_with_the_valid_list():
    rows, problems = ebay_cards.parse_rows([_raw(card_condition="MINT")])
    assert rows == []
    assert "NM" in problems[0] and "MINT" in problems[0]


def test_buy_it_now_must_exceed_start_price():
    rows, problems = ebay_cards.parse_rows([_raw(start_price_gbp="30.00", buy_it_now_price_gbp="30.00")])
    assert rows == []
    assert "buy_it_now_price_gbp must be greater than" in problems[0]


def test_zero_start_price_is_rejected():
    rows, problems = ebay_cards.parse_rows([_raw(start_price_gbp="0")])
    assert rows == []


def test_non_https_images_are_rejected():
    rows, problems = ebay_cards.parse_rows([_raw(image_urls="http://example.com/a.jpg")])
    assert rows == []
    assert "https://" in problems[0]


def test_missing_image_is_rejected():
    rows, problems = ebay_cards.parse_rows([_raw(image_urls="")])
    assert rows == []
    assert "image" in problems[0]


def test_duplicate_skus_are_rejected():
    rows, problems = ebay_cards.parse_rows([_raw(sku="A"), _raw(sku="A")])
    assert [r.sku for r in rows] == ["A"]
    assert len(problems) == 1


def test_one_bad_row_does_not_stop_the_good_ones():
    rows, problems = ebay_cards.parse_rows(
        [_raw(sku="A"), _raw(sku="B", card_condition="oops"), _raw(sku="C")])
    assert [r.sku for r in rows] == ["A", "C"]
    assert len(problems) == 1


# --- payload shapes ---------------------------------------------------------

def test_inventory_item_payload_shape():
    row = ebay_cards.parse_rows([_raw()])[0][0]
    item = ebay_cards.build_inventory_item(row)

    assert item["sku"] == "PC-001"
    # Trading cards repurpose the generic ConditionEnum: USED_VERY_GOOD
    # means "Ungraded" for this category, confirmed against a live 400
    # ("Could not serialize field [...condition]") when the raw numeric
    # conditionId (4000) was sent instead -- see module docstring.
    assert item["condition"] == "USED_VERY_GOOD"
    assert item["conditionDescriptors"] == [{"name": "40001", "values": ["400010"]}]
    assert item["availability"]["shipToLocationAvailability"]["quantity"] == 1
    assert item["product"]["imageUrls"] == [
        "https://example.com/a.jpg", "https://example.com/b.jpg"]


@pytest.mark.parametrize("condition,value_id", [
    ("NM", "400010"), ("LP", "400015"), ("MP", "400016"), ("HP", "400017"),
])
def test_condition_descriptor_maps_every_grade(condition, value_id):
    row = ebay_cards.parse_rows([_raw(card_condition=condition)])[0][0]
    item = ebay_cards.build_inventory_item(row)
    assert item["conditionDescriptors"][0]["values"] == [value_id]


def test_offer_payload_shape():
    row = ebay_cards.parse_rows([_raw()])[0][0]
    offer = ebay_cards.build_offer(row)

    assert offer["format"] == "AUCTION"
    assert offer["marketplaceId"] == "EBAY_GB"
    assert offer["categoryId"] == "183454"
    assert offer["listingDuration"] == "DAYS_7"
    # availableQuantity is rejected outright for AUCTION offers (errorId
    # 25762, confirmed live) -- must never be in this payload.
    assert "availableQuantity" not in offer
    assert offer["pricingSummary"]["price"]["value"] == "30.00"
    assert offer["pricingSummary"]["auctionStartPrice"]["value"] == "15.00"


# --- the run -----------------------------------------------------------

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
    rows, _ = ebay_cards.parse_rows([_raw(sku="A"), _raw(sku="B")])
    result = ebay_cards.list_cards(db_session, rows)

    assert sorted(result.published) == ["A", "B"]
    assert result.failed == []
    listing = db_session.get(models.EbayListing, "A")
    assert listing.status == "published"
    assert listing.offer_id == "OF-A"
    assert listing.listing_id == "L-OF-A"


def test_offer_failure_is_recorded(db_session, monkeypatch):
    _stub_ebay(monkeypatch, offer_ok=False)
    rows, _ = ebay_cards.parse_rows([_raw(sku="A")])
    result = ebay_cards.list_cards(db_session, rows)

    assert result.failed == ["A"]
    listing = db_session.get(models.EbayListing, "A")
    assert listing.status == "failed"
    assert "offer" in listing.last_error and "offer rejected" in listing.last_error


def test_rerun_skips_already_published_skus(db_session, monkeypatch):
    _stub_ebay(monkeypatch)
    rows, _ = ebay_cards.parse_rows([_raw(sku="A")])
    ebay_cards.list_cards(db_session, rows)

    def explode(*args, **kwargs):
        raise AssertionError("should not have called eBay again")

    monkeypatch.setattr(ebay_client, "bulk_create_offers", explode)
    result = ebay_cards.list_cards(db_session, rows)
    assert result.skipped == ["A"]
    assert result.published == []


def test_no_publish_stops_at_offer_created(db_session, monkeypatch):
    _stub_ebay(monkeypatch)
    monkeypatch.setattr(ebay_client, "bulk_publish_offers",
                        lambda ids: (_ for _ in ()).throw(AssertionError("should not publish")))
    rows, _ = ebay_cards.parse_rows([_raw(sku="A")])
    result = ebay_cards.list_cards(db_session, rows, publish=False)

    assert result.published == ["A"]
    assert db_session.get(models.EbayListing, "A").status == "offer_created"


def test_publish_failure_resumes_at_publish_without_a_second_offer(db_session, monkeypatch):
    """Same failure mode as ebay_lister's equivalent test: a SKU whose offer
    was created but whose publish failed must resume at stage 3 on retry --
    bulkCreateOffer is not idempotent, so re-creating the offer is an error,
    not a no-op."""
    _stub_ebay(monkeypatch, publish_ok=False)
    rows, _ = ebay_cards.parse_rows([_raw(sku="A")])
    ebay_cards.list_cards(db_session, rows)

    listing = db_session.get(models.EbayListing, "A")
    assert listing.status == "failed" and listing.offer_id == "OF-A"

    def explode(*args, **kwargs):
        raise AssertionError("must not re-create inventory or offers")

    monkeypatch.setattr(ebay_client, "bulk_create_or_replace_inventory_items", explode)
    monkeypatch.setattr(ebay_client, "bulk_create_offers", explode)
    monkeypatch.setattr(ebay_client, "bulk_publish_offers",
                        lambda ids: [ebay_client.BulkItemResult(
                            ok=True, status_code=200, offer_id=i, listing_id=f"L-{i}")
                            for i in ids])

    result = ebay_cards.list_cards(db_session, rows)
    assert result.published == ["A"]
    listing = db_session.get(models.EbayListing, "A")
    assert listing.status == "published" and listing.listing_id == "L-OF-A"


def test_dry_run_needs_no_database():
    rows, _ = ebay_cards.parse_rows([_raw(sku="A")])
    result = ebay_cards.list_cards(None, rows, dry_run=True)
    assert result.published == ["A"]
    assert result.notes and "dry run" in result.notes[0]
