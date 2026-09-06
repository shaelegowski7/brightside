"""CSV -> live eBay AUCTION listings for raw (ungraded) individual trading
cards. Sibling to ebay_lister.py, not an extension of it: manuals are
FIXED_PRICE with a Haynes-specific title/aspect scheme, cards are AUCTION
with a completely different condition model, so forcing both into one
ManualRow would have meant an optional-everything dataclass and branching
through every function. The three-stage resumable pipeline (inventory item
-> offer -> publish), the models.EbayListing tracking table, and every
ebay_client.py function are shared as-is; only the row shape and the two
build_* functions are new.

CONDITION IS NOT THE GENERIC ConditionEnum. eBay's CCG Individual Cards
category (183454) requires conditionId 4000 ("Ungraded") plus a REQUIRED
conditionDescriptors entry naming the raw grade -- confirmed against the
Metadata API's getItemConditionPolicies for category 183454, not assumed:

    4000 Ungraded
      descriptor 40001 "Card Condition" (REQUIRED, single-select)
        400010  Near mint or better
        400015  Lightly played (Excellent)
        400016  Moderately played (Very good)
        400017  Heavily played (Poor)

The conditionDescriptors field shape ({"name": descriptorId, "values":
[valueId]}) came from eBay's own docs but every reference page for it was
JS-rendered nav-only through both developer.ebay.com (403s scripts) and the
edp.ebay.com mirror (serves the same JS shell for this particular page) --
so this was verified empirically with a live createOrReplaceInventoryItem
call against CARD-TEST-001 rather than trusted blind. See that test's
result recorded in the module history / conversation, not repeated here.

AUCTION-specific pricing: PricingSummary's plain `price` field doubles as
the Buy-It-Now amount when format=AUCTION (there is no separate BIN field
in the schema); `auctionStartPrice` is the opening bid. Both were verified
against a live offer, not just the PHP client's model docs.
"""
import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from .config import get_ebay_settings
from . import ebay_client

if TYPE_CHECKING:
    from . import models

_MAX_TITLE_CHARS = 80
_CATEGORY_ID = "183454"  # Collectables > Collectable Card Games > CCG Individual Cards
# eBay repurposes the generic ConditionEnum names for trading cards/coins:
# USED_VERY_GOOD means "Ungraded" (conditionId 4000) here, not its usual
# meaning -- confirmed via a live 400 ("Could not serialize field
# [...condition]") when the literal numeric conditionId was sent instead.
# LIKE_NEW would mean "Graded" (conditionId 2750) if we ever listed one.
_CONDITION_ENUM = "USED_VERY_GOOD"
_CARD_CONDITION_DESCRIPTOR_ID = "40001"

CARD_CONDITIONS = {
    "NM": "400010",   # Near mint or better
    "LP": "400015",   # Lightly played (Excellent)
    "MP": "400016",   # Moderately played (Very good)
    "HP": "400017",   # Heavily played (Poor)
}


@dataclass
class CardRow:
    sku: str
    title: str
    card_condition: str          # NM / LP / MP / HP -- see CARD_CONDITIONS
    start_price_pence: int
    buy_it_now_price_pence: int
    listing_duration: str = "DAYS_7"
    quantity: int = 1
    description: str = ""
    image_urls: list[str] = field(default_factory=list)

    @property
    def start_price_string(self) -> str:
        return f"{self.start_price_pence / 100:.2f}"

    @property
    def buy_it_now_price_string(self) -> str:
        return f"{self.buy_it_now_price_pence / 100:.2f}"


def _parse_price_pence(raw: str) -> int | None:
    text = (raw or "").strip().lstrip("£").replace(",", "")
    try:
        value = round(float(text) * 100)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def parse_rows(raw_rows: list[dict]) -> tuple[list[CardRow], list[str]]:
    parsed: list[CardRow] = []
    problems: list[str] = []
    seen_skus: set[str] = set()

    for index, raw in enumerate(raw_rows, start=2):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        sku = row.get("sku", "")
        label = f"row {index}" + (f" ({sku})" if sku else "")

        if not sku:
            problems.append(f"{label}: missing sku")
            continue
        if sku in seen_skus:
            problems.append(f"{label}: duplicate sku")
            continue
        seen_skus.add(sku)

        card_condition = row.get("card_condition", "").upper()
        if card_condition not in CARD_CONDITIONS:
            problems.append(
                f"{label}: card_condition {row.get('card_condition', '')!r} not recognised "
                f"(use one of {', '.join(CARD_CONDITIONS)})"
            )
            continue

        start_price_pence = _parse_price_pence(row.get("start_price_gbp", ""))
        if start_price_pence is None:
            problems.append(f"{label}: start_price_gbp {row.get('start_price_gbp', '')!r} is not a positive number")
            continue

        bin_price_pence = _parse_price_pence(row.get("buy_it_now_price_gbp", ""))
        if bin_price_pence is None:
            problems.append(f"{label}: buy_it_now_price_gbp {row.get('buy_it_now_price_gbp', '')!r} is not a positive number")
            continue
        if bin_price_pence <= start_price_pence:
            problems.append(f"{label}: buy_it_now_price_gbp must be greater than start_price_gbp")
            continue

        title = row.get("title", "")
        if not title:
            problems.append(f"{label}: missing title")
            continue
        if len(title) > _MAX_TITLE_CHARS:
            problems.append(f"{label}: title is {len(title)} chars, eBay's limit is {_MAX_TITLE_CHARS}")
            continue

        images = [u.strip() for u in (row.get("image_urls") or "").split("|") if u.strip()]
        bad_images = [u for u in images if not u.lower().startswith("https://")]
        if bad_images:
            problems.append(
                f"{label}: image_urls must be public https:// URLs, got {bad_images[0]!r}"
            )
            continue
        if not images:
            problems.append(f"{label}: at least one image is required")
            continue

        parsed.append(CardRow(
            sku=sku, title=title, card_condition=card_condition,
            start_price_pence=start_price_pence, buy_it_now_price_pence=bin_price_pence,
            listing_duration=row.get("listing_duration") or "DAYS_7",
            description=row.get("description", "") or title,
            image_urls=images,
        ))

    return parsed, problems


def load_csv(path: str | Path) -> tuple[list[CardRow], list[str]]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return parse_rows(list(csv.DictReader(f)))


def build_inventory_item(row: CardRow) -> dict:
    """InventoryItemWithSkuLocale shape -- sku/locale/condition/product/
    availability/conditionDescriptors all as siblings, matching the bug
    fixed in ebay_lister.py's version of this function."""
    product = {
        "title": row.title,
        "description": row.description,
        "aspects": {"Game": ["Pokémon TCG"]},
        "imageUrls": row.image_urls,
    }
    return {
        "sku": row.sku,
        "locale": "en_GB",
        "condition": _CONDITION_ENUM,
        "conditionDescriptors": [
            {"name": _CARD_CONDITION_DESCRIPTOR_ID,
             "values": [CARD_CONDITIONS[row.card_condition]]},
        ],
        "product": product,
        "availability": {"shipToLocationAvailability": {"quantity": row.quantity}},
    }


def build_offer(row: CardRow) -> dict:
    s = get_ebay_settings()
    # availableQuantity is rejected outright for AUCTION offers (errorId
    # 25762) -- an auction is inherently one lot, quantity lives only on
    # the inventory item's availability, confirmed against a live 400.
    return {
        "sku": row.sku,
        "marketplaceId": s.marketplace_id,
        "format": "AUCTION",
        "categoryId": _CATEGORY_ID,
        "listingDescription": row.description,
        "listingDuration": row.listing_duration,
        "merchantLocationKey": s.merchant_location_key,
        "listingPolicies": {
            "fulfillmentPolicyId": s.fulfillment_policy_id,
            "paymentPolicyId": s.payment_policy_id,
            "returnPolicyId": s.return_policy_id,
        },
        "pricingSummary": {
            "price": {"value": row.buy_it_now_price_string, "currency": "GBP"},
            "auctionStartPrice": {"value": row.start_price_string, "currency": "GBP"},
        },
    }


# --------------------------------------------------------------------------
# The run -- deliberately mirrors ebay_lister.list_manuals's three-stage,
# resumable structure. See that function's comments for why each stage is
# recorded before the next starts.
# --------------------------------------------------------------------------

@dataclass
class RunResult:
    published: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"{len(self.published)} published, {len(self.failed)} failed, "
                f"{len(self.skipped)} already done, {len(self.problems)} blocked")


def _record(db: Session, row: CardRow) -> "models.EbayListing":
    from . import models
    listing = db.get(models.EbayListing, row.sku)
    if listing is None:
        listing = models.EbayListing(
            sku=row.sku, title=row.title, condition=row.card_condition,
            price_pence=row.buy_it_now_price_pence, quantity=row.quantity,
            category_id=_CATEGORY_ID, status="pending",
        )
        db.add(listing)
        db.flush()
    return listing


def _fail(listing: "models.EbayListing", stage: str, errors: list[str]) -> None:
    listing.status = "failed"
    listing.last_error = f"{stage}: {'; '.join(errors)[:1000]}"


def _has_offer(db: Session, sku: str) -> bool:
    from . import models
    listing = db.get(models.EbayListing, sku)
    return bool(listing is not None and listing.offer_id)


def list_cards(db: Session | None, rows: list[CardRow], *, publish: bool = True,
                dry_run: bool = False) -> RunResult:
    result = RunResult()

    if dry_run:
        for row in rows:
            build_inventory_item(row)
            build_offer(row)
        result.published = [r.sku for r in rows]
        result.notes = [f"dry run -- {len(rows)} payload(s) built, nothing sent to eBay"]
        return result

    from . import models
    assert db is not None, "a real run needs a database session"

    missing = ebay_client.publishing_prerequisites_missing()
    if publish and missing:
        result.problems.append(
            "cannot publish -- unset: " + ", ".join(missing)
        )
        return result

    for row in rows:
        _record(db, row)
    db.commit()

    already_done = [r for r in rows if db.get(models.EbayListing, r.sku).status == "published"]
    result.skipped = [r.sku for r in already_done]
    todo = [r for r in rows if r.sku not in result.skipped]
    # Built from todo, not rows -- an already-skipped SKU must not also
    # show up in the final published/failed accounting below.
    by_sku = {r.sku: r for r in todo}

    # ---- stage 1: inventory items ---------------------------------------
    stage1_rows = [r for r in todo if not _has_offer(db, r.sku)]
    if stage1_rows:
        for res in ebay_client.bulk_create_or_replace_inventory_items(
                [build_inventory_item(r) for r in stage1_rows]):
            listing = db.get(models.EbayListing, res.sku) if res.sku else None
            if listing is None:
                continue
            if res.ok:
                listing.status = "inventory_created"
                listing.last_error = None
            else:
                _fail(listing, "inventory", res.errors)
        db.commit()

    # ---- stage 2: offers --------------------------------------------------
    stage2_rows = [r for r in todo
                   if db.get(models.EbayListing, r.sku).status == "inventory_created"
                   and not _has_offer(db, r.sku)]
    if stage2_rows:
        for res in ebay_client.bulk_create_offers(
                [build_offer(r) for r in stage2_rows]):
            listing = db.get(models.EbayListing, res.sku) if res.sku else None
            if listing is None:
                continue
            if res.ok and res.offer_id:
                listing.offer_id = res.offer_id
                listing.status = "offer_created"
                listing.last_error = None
            else:
                _fail(listing, "offer", res.errors)
        db.commit()

    # ---- stage 3: publish ---------------------------------------------
    if publish:
        pending = [db.get(models.EbayListing, r.sku) for r in todo]
        to_publish = [x for x in pending if x is not None
                      and x.offer_id and x.status != "published"]
        if to_publish:
            by_offer = {x.offer_id: x for x in to_publish}
            for res in ebay_client.bulk_publish_offers([x.offer_id for x in to_publish]):
                listing = by_offer.get(res.offer_id)
                if listing is None:
                    continue
                if res.ok:
                    listing.status = "published"
                    listing.listing_id = res.listing_id
                    listing.last_error = None
                else:
                    _fail(listing, "publish", res.errors)
            db.commit()

    for sku in by_sku:
        listing = db.get(models.EbayListing, sku)
        target = "published" if publish else "offer_created"
        if listing is not None and listing.status == target:
            result.published.append(sku)
        else:
            result.failed.append(sku)
    return result
