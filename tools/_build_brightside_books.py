"""One-off: turns the handwritten BRIGHTSIDE BOOKS stock list into the
lister's CSV format. Kept in the repo so the transcription is reviewable
and re-runnable rather than being a thing that happened once in a chat.

Splitting rule: one row per distinct *sellable* unit. Copies that differ in
condition or in variant (petrol vs diesel, hardback vs standard) become
separate rows, because they cannot share a listing. Truly identical copies
stay on one row with quantity N, which also spends fewer of eBay's monthly
listing slots.

CONFIRMED WITH SHAE 2026-08-31:
- Every book is NEW (new old stock), not used -- the one exception is the
  BMW 3 Series 1998-2006 they themselves marked "damaged", left as
  USED_ACCEPTABLE and flagged for a decision rather than silently called
  new. Note that eBay only permits conditionDescription on *used*
  conditions, so product facts ("also covers X", "hardback") go in the
  `description` column instead, where they belong anyway.
- "petrol & diesel" describes one manual covering BOTH fuels, so those
  stay as a single row with quantity N. The Ford Focus 2005-2011 line is
  the one exception: "1 petrol 1 diesel" is explicitly one of each, so it
  stays split into two rows.
- Every book is Haynes-branded, so the hardcoded Brand aspect is right.
- The Citroen is an Xsara Picasso.

"NEEDS SEALED" IS NOT A CONDITION. Confirmed with Shae 2026-08-31: those
copies are fine, they are simply not shrink-wrapped yet (there is a sealer
in-house) and are currently stored apart from the rest of the stock. So
they carry the same condition as everything else, they merge back onto the
same row as their identical siblings, and the note lives in `packing_note`
-- a column the lister does not read and never sends to eBay. Putting it in
condition_description instead would advertise damage that does not exist.
"""
import csv

# make, model, year_from, year_to, qty, variant, condition,
# extra (product fact -> description; for the damaged book, condition text),
# packing_note (internal only), photo_ids (IMG_ numbers from the iCloud zip)
STOCK = [
    ("Audi", "A3", 2003, 2008, 1, "", "NEW", "", "", "6781,6782"),
    ("Audi", "A4", 2005, 2008, 1, "", "NEW", "", "", "6777,6778"),
    ("BMW", "3 Series", 2005, 2008, 3, "", "NEW", "", "", ""),
    ("BMW", "3 Series", 1998, 2006, 1, "", "USED_ACCEPTABLE", "Damaged.", "", ""),
    ("BMW", "3 Series", 2008, 2012, 1, "", "NEW", "", "", "6859,6860"),
    ("BMW", "5 Series", 2003, 2010, 1, "", "NEW", "", "", "7006,7007"),
    ("BMW", "5 Series", 2003, 2010, 1, "Hardback", "NEW", "Hardback edition.", "", ""),
    ("Citroen", "Berlingo", 1996, 2010, 1, "", "NEW",
     "Also covers Peugeot Partner.", "", "6706"),
    ("Citroen", "C4", 2004, 2010, 1, "", "NEW", "", "", "6715,7014,7015"),
    ("Citroen", "Dispatch", 2007, 2016, 1, "", "NEW",
     "Also covers Peugeot Expert and Fiat Scudo.", "", "6761,6762,6798,6799"),
    ("Citroen", "Xsara Picasso", 2004, 2010, 2, "", "NEW", "", "", "6747,6748"),
    ("Ford", "C-Max", 2003, 2010, 2, "", "NEW", "", "", "6732,6767,6768"),
    ("Ford", "Focus", 2001, 2005, 3, "", "NEW", "", "", "6983,6984,6997,6998"),
    ("Ford", "Focus", 2005, 2011, 1, "Petrol", "NEW", "", "", ""),
    ("Ford", "Focus", 2005, 2011, 1, "Diesel", "NEW", "", "", "6751,6752"),
    ("Ford", "Focus", 2011, 2014, 1, "", "NEW", "", "", ""),
    ("Ford", "Fusion", 2002, 2012, 3, "", "NEW", "", "", "6771,6772"),
    ("Ford", "Ka", 1996, 2008, 5, "", "NEW", "", "", "6734,6783,6784"),
    ("Ford", "Ka", 2009, 2014, 2, "", "NEW", "", "", "6985,6986"),
    ("Ford", "Mondeo", 2003, 2007, 2, "", "NEW", "", "", "6709,6973,6974"),
    ("Ford", "Transit Connect", 2002, 2011, 2, "", "NEW", "", "", "6749,6750"),
    ("Ford", "Transit", 2006, 2013, 1, "Diesel", "NEW", "", "", "6730,6731"),
    ("Ford", "S-Max & Galaxy", 2006, 2015, 1, "", "NEW", "", "", "6718,6719,6765,6766"),
    ("Fiat", "500 & Panda", 2004, 2012, 1, "", "NEW", "", "", ""),
    ("Fiat", "Grande Punto & Punto Evo", 2006, 2015, 1, "", "NEW", "", "", "6713,6714,6779,6780"),
    ("Honda", "Civic", 2006, 2012, 2, "", "NEW", "", "", ""),
    ("Honda", "CR-V", 2002, 2006, 2, "", "NEW", "", "1 of 2 needs sealing", "6867,6868"),
    ("Land Rover", "Defender", 2007, 2016, 1, "", "NEW", "", "", ""),
    ("Land Rover", "Discovery", 1989, 1998, 1, "", "NEW", "", "", ""),
    ("Land Rover", "Discovery", 2004, 2009, 2, "Diesel", "NEW", "", "", ""),
    ("Land Rover", "Freelander", 1997, 2006, 2, "Petrol & Diesel", "NEW", "", "", "6745,6746"),
    ("Mini", "", 2014, 2018, 2, "", "NEW", "", "", ""),
    ("Nissan", "Note", 2006, 2013, 2, "Petrol & Diesel", "NEW", "", "", "6739,6740,6759,6760,7020,7021"),
    ("Peugeot", "206", 2002, 2009, 1, "Petrol & Diesel", "NEW", "", "", "6773,6774"),
    ("Peugeot", "308", 2007, 2013, 1, "", "NEW", "", "", "6726,6727,6787,6788,6954,6955,6979,6980"),
    ("Renault", "Clio", 2001, 2005, 1, "Petrol & Diesel", "NEW", "", "", "6963,6964"),
    ("Renault", "Clio", 2001, 2005, 2, "Hardback", "NEW",
     "Hardback edition.", "both need sealing", "6999,7001"),
    ("Renault", "Clio", 2005, 2009, 1, "Petrol & Diesel", "NEW", "", "", "6711,6712"),
    ("Renault", "Clio", 2009, 2012, 1, "Petrol & Diesel", "NEW", "", "", ""),
    ("Renault", "Megane", 2002, 2008, 3, "Petrol & Diesel", "NEW", "",
     "1 of 3 needs sealing", "6977,6978"),
    ("Saab", "9-3", 2002, 2007, 1, "Petrol & Diesel", "NEW", "", "", "6724,6725"),
    ("Skoda", "Octavia", 2004, 2013, 1, "Diesel", "NEW", "", "needs sealing", ""),
    ("Toyota", "Aygo, Peugeot 107 & Citroen C1", 2005, 2014, 1, "Petrol",
     "NEW", "", "", "6769,6770,6800"),
    ("Vauxhall", "Astra", 2004, 2008, 2, "Diesel", "NEW", "",
     "1 of 2 needs sealing", "6755,6756"),
    ("Vauxhall", "Astra", 2004, 2008, 1, "Petrol", "NEW", "", "", "6993,6994,7012,7013"),
    ("Vauxhall", "Corsa", 2006, 2010, 1, "Petrol & Diesel", "NEW", "", "", ""),
    ("Vauxhall", "Combo Van", 2001, 2012, 2, "Diesel", "NEW", "",
     "1 of 2 needs sealing", "6775,6776"),
    ("Vauxhall", "Vectra", 2005, 2008, 2, "Petrol & Diesel", "NEW", "", "", "7004,7005"),
    ("Vauxhall", "Zafira", 2005, 2009, 3, "Petrol & Diesel", "NEW", "", "", "6743,6967,6968,6989,6990"),
    ("Volvo", "S40 & V50", 2004, 2013, 2, "Petrol & Diesel", "NEW", "",
     "1 of 2 needs sealing", ""),
    ("Volvo", "XC60 & XC90", 2003, 2013, 1, "Diesel", "NEW", "", "", "6796,6797"),
    ("VW", "Golf", 2009, 2012, 1, "Petrol & Diesel", "NEW", "", "", ""),
    ("VW", "Golf & Bora", 2001, 2003, 1, "Petrol & Diesel", "NEW", "", "", "6704,6705,7018,7019"),
    ("VW", "Passat", 2005, 2010, 2, "Diesel", "NEW", "", "", "6741,6742"),
    ("VW", "Passat", 2011, 2014, 1, "Diesel", "NEW", "", "", ""),
    ("VW", "Polo", 2002, 2009, 2, "Petrol & Diesel", "NEW", "", "", ""),
    ("VW", "Touran", 2003, 2015, 1, "Diesel", "NEW", "", "", ""),
]

# Flat £20 across the batch (Shae, 2026-08-31). These are new old stock,
# not used, which is what supports a single price rather than a per-title
# one. Override per row in the CSV if a title turns out to be scarce.
FLAT_PRICE_GBP = "20.00"

# Cover RRP, quoted in the description as the anchor for the £20 price.
# Deliberately NOT sent as eBay's PricingSummary.originalRetailPrice: that
# field drives eBay's strikethrough-pricing display, which is gated on
# seller eligibility and has its own evidence rules. Stating the RRP as
# plain description text is accurate and carries no such gate.
RRP_GBP = "24.00"

# eBay UK "Car Service & Repair Manuals". Derived 2026-08-31 from eBay's own
# public browse URLs, which take the form /b/<Name>/<categoryId>/bn_<facet>:
#   /b/Car-Service-Repair-Manuals/183721/bn_2314060
#   /b/1960-Car-Service-Repair-Manuals/183721/bn_79264587
#   /b/Parts-Catalogues-Car-Service-Repair-Manuals/183721/bn_79263710
# All three share 183721 while differing in the bn_ suffix, which says the
# year and literature-type views are facets rather than child categories --
# i.e. 183721 is a leaf, and eBay only accepts leaf categories on a listing.
#
# NOT confirmed against the Taxonomy API, which needs credentials we do not
# have yet. Re-check once the keyset lands, before publishing:
#   python -m tools.ebay_consent categories "Haynes manual"
CATEGORY_ID = "183721"

MAX_TITLE = 80


def build_title(make, model, yf, yt, variant):
    """Longest form that still fits eBay's 80-char title limit. Buyers
    search make/model/years, so those never get trimmed -- the marketing
    suffix goes first."""
    head = " ".join(p for p in ["Haynes Manual", make, model, f"{yf}-{yt}", variant] if p)
    for suffix in (" Service & Repair Workshop", " Service & Repair", " Workshop Manual", ""):
        title = head + suffix
        if len(title) <= MAX_TITLE:
            return title
    return head[:MAX_TITLE].rstrip()


def main():
    rows = []
    for index, entry in enumerate(STOCK, start=1):
        make, model, yf, yt, qty, variant, condition, extra, packing, photos = entry
        title = build_title(make, model, yf, yt, variant)

        # eBay permits conditionDescription only on *used* conditions, and
        # everything here is NEW bar one book -- so a product fact ("also
        # covers X", "hardback edition") has to go in the listing
        # description or it is silently dropped. Only the genuinely used
        # book puts its text in condition_description, which is what that
        # field is actually for.
        is_used = condition != "NEW"
        parts = [f"{title}."]
        if extra and not is_used:
            parts.append(extra)
        parts.append("Haynes workshop manual covering routine maintenance, "
                     "servicing and repair.")
        if not is_used:
            parts.append(f"Brand new and still sealed. RRP £{RRP_GBP}.")
        description = " ".join(parts)

        rows.append({
            "sku": f"BB-{index:03d}",
            "title": title,
            "make": make,
            "model": " ".join(p for p in [model, variant] if p),
            "year_from": yf,
            "year_to": yt,
            "haynes_number": "",
            "isbn": "",
            "condition": condition,
            "price_gbp": FLAT_PRICE_GBP,
            "quantity": qty,
            "category_id": CATEGORY_ID,
            "description": description,
            "condition_description": extra if is_used else "",
            "image_urls": "",
            # Internal only -- ebay_lister ignores unknown columns, so this
            # never reaches eBay. Stock-room note, not a listing field.
            "packing_note": packing,
            # Which IMG_ numbers in the iCloud zip show this book. Internal:
            # the lister ignores it. Empty means NOT PHOTOGRAPHED, which
            # means it cannot be listed at all -- eBay requires an image.
            "photo_ids": photos,
        })

    with open("brightside_books.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    copies = sum(r["quantity"] for r in rows)
    sealing = [r["sku"] for r in rows if r["packing_note"]]
    print(f"{len(rows)} rows, {copies} physical copies")
    print(f"{len(sealing)} row(s) carry a sealing note: {', '.join(sealing)}")
    longest = max(rows, key=lambda r: len(r["title"]))
    print(f"longest title: {len(longest['title'])} chars -- {longest['title']}")


if __name__ == "__main__":
    main()
