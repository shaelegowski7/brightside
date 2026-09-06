"""CSV -> live eBay listings, in the three stages eBay's Inventory API
requires. Built for the Haynes manual batch but nothing here is
manual-specific beyond the default aspects.

    stage 1  bulk_create_or_replace_inventory_item   what the product IS
    stage 2  bulk_create_offer                       what it costs, where
    stage 3  bulk_publish_offer                      make it live

Each stage is recorded per-SKU in models.EbayListing before the next
starts, which is what makes a re-run resumable: a row already at
'published' is skipped, and a row that died at 'offer_created' picks up at
stage 3 rather than creating a duplicate offer. That matters more than
usual here because stage 2 is NOT idempotent -- calling bulk_create_offer
twice for one SKU on one marketplace is an error from eBay, not a no-op,
so the resume path is the only safe way to retry a partial batch.

IMAGES ARE THE USUAL BLOCKER. The Inventory API takes image *URLs*, not
uploads -- every URL in the image_urls column must already be publicly
reachable over HTTPS before you run this. Local paths, http://, and
anything behind auth are rejected at validation time here rather than
failing halfway through a batch. (eBay's own image hosting is only
reachable through the legacy Trading API's UploadSiteHostedPictures, which
this module deliberately does not pull in -- host them anywhere public.)
"""
import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from . import ebay_client
from .config import get_ebay_settings

# app.models pulls in app.database, which builds the SQLAlchemy engine at
# import time and so hard-requires DATABASE_URL and the Supabase secrets.
# Importing it lazily is what lets --dry-run validate a sheet on a machine
# where none of that is configured; every runtime use is inside a function.
if TYPE_CHECKING:
    from . import models

# eBay's hard limit; a longer title is rejected at publish, so it is caught
# during validation instead -- one bad row should not stop a batch of 100.
_MAX_TITLE_CHARS = 80
_MAX_SUBTITLE_CHARS = 55

# ConditionEnum values, confirmed 2026-08-31 against eBay's condition ID
# reference (www.edp.ebay.com/api-docs/sell/static/metadata/
# condition-id-values.html). The numeric IDs are shown because that is what
# eBay's own UI and the condition reference use; the API takes the name.
# The wording for USED_GOOD / USED_ACCEPTABLE in that reference is written
# explicitly around books ("the majority of pages have minimal damage or
# markings and no missing pages"), which is what these are.
CONDITIONS = {
    "NEW": 1000,
    "LIKE_NEW": 2750,
    "USED_EXCELLENT": 3000,
    "USED_VERY_GOOD": 4000,
    "USED_GOOD": 5000,
    "USED_ACCEPTABLE": 6000,
}

# Forgiving input -- the spreadsheet will be typed by a human, not generated.
_CONDITION_ALIASES = {
    "new": "NEW",
    "like new": "LIKE_NEW",
    "as new": "LIKE_NEW",
    "excellent": "USED_EXCELLENT",
    "very good": "USED_VERY_GOOD",
    "vg": "USED_VERY_GOOD",
    "good": "USED_GOOD",
    "used": "USED_GOOD",
    "acceptable": "USED_ACCEPTABLE",
    "poor": "USED_ACCEPTABLE",
}

REQUIRED_COLUMNS = ("sku", "condition", "price_gbp")


@dataclass
class ManualRow:
    sku: str
    title: str
    condition: str
    price_pence: int
    quantity: int = 1
    isbn: str = ""
    haynes_number: str = ""
    make: str = ""
    model: str = ""
    year_from: str = ""
    year_to: str = ""
    category_id: str = ""
    subtitle: str = ""
    description: str = ""
    condition_description: str = ""
    image_urls: list[str] = field(default_factory=list)

    @property
    def price_string(self) -> str:
        """eBay's Amount.value is a string, not a number."""
        return f"{self.price_pence / 100:.2f}"


def _normalise_condition(raw: str) -> str | None:
    value = (raw or "").strip()
    if value.upper() in CONDITIONS:
        return value.upper()
    return _CONDITION_ALIASES.get(value.lower())


def _default_title(row: dict) -> str:
    """Builds a searchable title when the sheet leaves one blank. eBay
    buyers search by make/model/years, so lead with those."""
    make = (row.get("make") or "").strip()
    model = (row.get("model") or "").strip()
    y_from = (row.get("year_from") or "").strip()
    y_to = (row.get("year_to") or "").strip()
    years = f"{y_from}-{y_to}" if y_from and y_to else (y_from or y_to)
    parts = ["Haynes Manual", make, model, years, "Service Repair Workshop"]
    return " ".join(p for p in parts if p).strip()


def _parse_price_pence(raw: str) -> int | None:
    text = (raw or "").strip().lstrip("£").replace(",", "")
    try:
        value = round(float(text) * 100)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def parse_rows(raw_rows: list[dict]) -> tuple[list[ManualRow], list[str]]:
    """Validates everything eBay would reject, up front. Returns the good
    rows and a human-readable problem per bad row -- a batch of 100 should
    list the 97 that are fine rather than stopping at the first typo."""
    parsed: list[ManualRow] = []
    problems: list[str] = []
    seen_skus: set[str] = set()

    for index, raw in enumerate(raw_rows, start=2):   # row 1 is the header
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

        condition = _normalise_condition(row.get("condition", ""))
        if condition is None:
            problems.append(
                f"{label}: condition {row.get('condition', '')!r} not recognised "
                f"(use one of {', '.join(CONDITIONS)})"
            )
            continue

        price_pence = _parse_price_pence(row.get("price_gbp", ""))
        if price_pence is None:
            problems.append(f"{label}: price_gbp {row.get('price_gbp', '')!r} is not a positive number")
            continue

        title = row.get("title") or _default_title(row)
        if len(title) > _MAX_TITLE_CHARS:
            problems.append(f"{label}: title is {len(title)} chars, eBay's limit is {_MAX_TITLE_CHARS}")
            continue

        subtitle = row.get("subtitle", "")
        if len(subtitle) > _MAX_SUBTITLE_CHARS:
            problems.append(f"{label}: subtitle is {len(subtitle)} chars, limit is {_MAX_SUBTITLE_CHARS}")
            continue

        try:
            quantity = int(row.get("quantity") or 1)
        except ValueError:
            problems.append(f"{label}: quantity {row.get('quantity')!r} is not a whole number")
            continue
        if quantity < 1:
            problems.append(f"{label}: quantity must be at least 1")
            continue

        images = [u.strip() for u in (row.get("image_urls") or "").split("|") if u.strip()]
        bad_images = [u for u in images if not u.lower().startswith("https://")]
        if bad_images:
            problems.append(
                f"{label}: image_urls must be public https:// URLs, got {bad_images[0]!r} "
                "(eBay fetches images by URL; it cannot read local files)"
            )
            continue

        parsed.append(ManualRow(
            sku=sku, title=title, condition=condition, price_pence=price_pence,
            quantity=quantity, isbn=row.get("isbn", ""),
            haynes_number=row.get("haynes_number", ""), make=row.get("make", ""),
            model=row.get("model", ""), year_from=row.get("year_from", ""),
            year_to=row.get("year_to", ""), category_id=row.get("category_id", ""),
            subtitle=subtitle, description=row.get("description", ""),
            condition_description=row.get("condition_description", ""),
            image_urls=images,
        ))

    return parsed, problems


def load_csv(path: str | Path) -> tuple[list[ManualRow], list[str]]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = {(h or "").strip().lower() for h in (reader.fieldnames or [])}
        missing = [c for c in REQUIRED_COLUMNS if c not in headers]
        if missing:
            return [], [f"CSV is missing required column(s): {', '.join(missing)}"]
        return parse_rows(list(reader))


# --------------------------------------------------------------------------
# Payload building
# --------------------------------------------------------------------------

def _aspects(row: ManualRow) -> dict[str, list[str]]:
    """eBay item specifics. Values are always *lists* of strings, even for
    a single value -- a bare string is rejected. Only non-empty aspects are
    sent; an empty list is itself an error."""
    aspects = {"Brand": ["Haynes"], "Type": ["Repair Manual"], "Format": ["Paperback"]}
    if row.make:
        aspects["Make"] = [row.make]
    if row.model:
        aspects["Model"] = [row.model]
    if row.year_from:
        aspects["Year"] = [row.year_from] if not row.year_to else [f"{row.year_from}-{row.year_to}"]
    if row.haynes_number:
        aspects["Manufacturer Part Number"] = [row.haynes_number]
    return aspects


def _description(row: ManualRow) -> str:
    lines = [row.description] if row.description else [
        f"{row.title}.",
        "Haynes workshop manual covering routine maintenance, servicing and repair.",
    ]
    if row.haynes_number:
        lines.append(f"Haynes manual number: {row.haynes_number}")
    if row.isbn:
        lines.append(f"ISBN: {row.isbn}")
    if row.condition_description:
        lines.append(f"Condition: {row.condition_description}")
    return "<br>".join(lines)


def build_inventory_item(row: ManualRow) -> dict:
    product = {
        "title": row.title,
        "description": _description(row),
        "aspects": _aspects(row),
    }
    if row.image_urls:
        product["imageUrls"] = row.image_urls
    if row.subtitle:
        product["subtitle"] = row.subtitle
    # ISBN is a first-class product identifier on eBay, and Haynes manuals
    # all carry one -- supplying it lets eBay match its own catalogue entry
    # and fill in item specifics we would otherwise have to type.
    if row.isbn:
        product["isbn"] = [row.isbn]
    if row.haynes_number:
        product["mpn"] = row.haynes_number

    item: dict = {
        "condition": row.condition,
        "product": product,
        "availability": {"shipToLocationAvailability": {"quantity": row.quantity}},
    }
    if row.condition_description and row.condition != "NEW":
        # eBay only permits conditionDescription on used conditions.
        item["conditionDescription"] = row.condition_description
    # bulkCreateOrReplaceInventoryItem's request items are shaped as
    # InventoryItemWithSkuLocale: sku/locale/condition/product/availability
    # are ALL siblings -- there is no nested "inventoryItem" wrapper, unlike
    # what this shape used to be. eBay silently reads an unrecognised
    # wrapper as an empty item (errorId 25002, "payload is empty") rather
    # than rejecting the unknown key, which made this bug invisible short of
    # an actual API call.
    item["sku"] = row.sku
    item["locale"] = "en_GB"
    return item


def build_offer(row: ManualRow, category_id: str = "") -> dict:
    s = get_ebay_settings()
    category = row.category_id or category_id
    return {
        "sku": row.sku,
        "marketplaceId": s.marketplace_id,
        "format": "FIXED_PRICE",
        "availableQuantity": row.quantity,
        "categoryId": category,
        "listingDescription": _description(row),
        "merchantLocationKey": s.merchant_location_key,
        "listingPolicies": {
            "fulfillmentPolicyId": s.fulfillment_policy_id,
            "paymentPolicyId": s.payment_policy_id,
            "returnPolicyId": s.return_policy_id,
            # Best Offer on by default: for one-off used stock, an offer is
            # usually the difference between a sale and a listing that sits.
            "bestOfferTerms": {"bestOfferEnabled": True},
        },
        "pricingSummary": {"price": {"value": row.price_string, "currency": "GBP"}},
    }


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------

@dataclass
class RunResult:
    published: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    # Blockers -- something was wrong and nothing (or less) was sent.
    problems: list[str] = field(default_factory=list)
    # Informational; kept apart from problems so a clean dry run does not
    # report itself as a rejection.
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"{len(self.published)} published, {len(self.failed)} failed, "
                f"{len(self.skipped)} already done, {len(self.problems)} blocked")


def _record(db: Session, row: ManualRow) -> "models.EbayListing":
    from . import models

    listing = db.get(models.EbayListing, row.sku)
    if listing is None:
        listing = models.EbayListing(sku=row.sku)
        db.add(listing)
    listing.title = row.title
    listing.isbn = row.isbn or None
    listing.condition = row.condition
    listing.price_pence = row.price_pence
    listing.quantity = row.quantity
    listing.category_id = row.category_id or None
    if listing.status is None:
        listing.status = "pending"
    return listing


def _fail(listing: "models.EbayListing", stage: str, errors: list[str]) -> None:
    listing.status = "failed"
    listing.last_error = f"{stage}: " + ("; ".join(errors) if errors else "unknown error")


def list_manuals(db: Session | None, rows: list[ManualRow], *, category_id: str = "",
                 publish: bool = True, dry_run: bool = False) -> RunResult:
    """Runs the three stages over `rows`, recording progress per SKU.

    dry_run builds and validates every payload without calling eBay *or*
    the database -- `db` may be None. That is deliberate: the point of a
    dry run is to check a 100-row sheet on a laptop before spending real
    listing slots, and requiring a live Postgres connection to do it would
    defeat the purpose."""
    result = RunResult()

    if not rows:
        return result

    # Before anything with a side effect, including the DB write below.
    if dry_run:
        for row in rows:
            build_inventory_item(row)
            build_offer(row, category_id)
        result.notes.append(f"dry run -- {len(rows)} payload(s) built, nothing sent to eBay")
        return result

    if db is None:
        raise ValueError("db is required unless dry_run=True")

    from . import models

    missing = ebay_client.publishing_prerequisites_missing()
    if publish and missing:
        result.problems.append(
            "cannot publish -- these are unset: " + ", ".join(missing)
            + " (see docs/EBAY_SETUP.md step 6)"
        )
        return result

    without_category = [r.sku for r in rows if not (r.category_id or category_id)]
    if without_category:
        result.problems.append(
            f"{len(without_category)} row(s) have no categoryId and no default was given "
            f"(first: {without_category[0]}) -- find one with "
            "`python -m tools.ebay_consent categories \"Haynes manual\"`"
        )
        return result

    todo: list[ManualRow] = []
    for row in rows:
        listing = _record(db, row)
        if listing.status == "published":
            result.skipped.append(row.sku)
            continue
        todo.append(row)
    db.commit()

    # An existing offer_id means stages 1 and 2 are already done for that
    # SKU, whatever `status` says -- a row that failed at *publish* is
    # marked 'failed' but still owns a live unpublished offer. Sending it
    # back through stage 2 would ask eBay to create a second offer for the
    # same SKU, which is an error from eBay rather than a no-op. So
    # offer_id, not status, is what gates the two earlier stages.
    def _has_offer(sku: str) -> bool:
        listing = db.get(models.EbayListing, sku)
        return bool(listing is not None and listing.offer_id)

    # ---- stage 1: inventory items -------------------------------------
    by_sku = {r.sku: r for r in todo}
    stage1_rows = [r for r in todo
                   if db.get(models.EbayListing, r.sku).status in ("pending", "failed")
                   and not _has_offer(r.sku)]
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

    # ---- stage 2: offers ----------------------------------------------
    stage2_rows = [r for r in todo
                   if db.get(models.EbayListing, r.sku).status == "inventory_created"
                   and not _has_offer(r.sku)]
    if stage2_rows:
        for res in ebay_client.bulk_create_offers(
                [build_offer(r, category_id) for r in stage2_rows]):
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
        # Anything holding an offer that never went live -- including a
        # row left at 'failed' by a previous publish attempt.
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
