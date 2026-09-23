"""One-off builder for the Pokemon card auction batch CSV. Not part of the
app -- run once, keep the output (pokemon_cards.csv) as the source of
truth from here.
"""
import csv

IMG = "https://brightside-images.vercel.app/IMG_{}.JPEG"

# (sku, title, card_condition, front_id, back_id, start_gbp, nm_est_gbp, description)
CARDS = [
    ("PC-001", "Vaporeon EX 24/83 Generations Radiant Collection Holo Pokemon", "NM", "7493", "7494", 15, 12,
     "Vaporeon EX 24/83, Generations Radiant Collection. Raw, ungraded."),
    ("PC-002", "Dragonite EX 106/108 XY Evolutions Gold Full Art Secret Rare", "NM", "7495", "7496", 25, 32,
     "Dragonite EX 106/108, XY Evolutions, gold full art secret rare. Raw, ungraded."),
    ("PC-003", "M Gardevoir EX 112/114 XY Steam Siege Full Art Pokemon", "NM", "7497", "7498", 18, 16,
     "M Gardevoir EX 112/114, XY Steam Siege, full art. Raw, ungraded."),
    ("PC-004", "M Rayquaza EX 105/108 XY Roaring Skies Full Art Pokemon", "MP", "7501", "7502", 40, 265,
     "M Rayquaza EX 105/108, XY Roaring Skies, full art. Raw, ungraded. Visible crease -- see photos, priced accordingly."),
    ("PC-005", "Snorlax GX SM05 Black Star Promo Pokemon", "NM", "7503", "7504", 15, 6,
     "Snorlax GX, SM05 Black Star Promo. Raw, ungraded."),
    ("PC-006", "M Gardevoir EX RC31 Generations Radiant Collection Full Art", "NM", "7505", "7506", 15, 12,
     "M Gardevoir EX RC31, Generations Radiant Collection, full art. Raw, ungraded."),
    ("PC-007", "Glaceon EX 20/124 XY Fates Collide Holo Pokemon", "NM", "7507", "7508", 15, 6,
     "Glaceon EX 20/124, XY Fates Collide. Raw, ungraded."),
    ("PC-008", "Umbreon EX 119/124 XY Fates Collide Full Art Pokemon", "LP", "7509", "7510", 35, 79,
     "Umbreon EX 119/124, XY Fates Collide, full art. Raw, ungraded. Minor edge/corner wear -- see photos."),
    ("PC-009", "Professor Sycamore 114/114 XY Steam Siege Full Art Trainer", "NM", "7511", "7512", 15, 8,
     "Professor Sycamore 114/114, XY Steam Siege, full art secret rare. Raw, ungraded."),
    ("PC-010", "M Pidgeot EX 65/108 XY Evolutions Full Art Pokemon", "LP", "7513", "7514", 15, 12,
     "M Pidgeot EX 65/108, XY Evolutions, full art. Raw, ungraded. Small crease -- see photos, priced accordingly."),
    ("PC-011", "Alakazam EX 25/124 XY Fates Collide Holo Pokemon", "NM", "7515", "7516", 15, 5,
     "Alakazam EX 25/124, XY Fates Collide. Raw, ungraded."),
    ("PC-012", "M Charizard EX XY Evolutions Full Art Secret Rare Pokemon", "NM", "7517", "7518", 28, 37,
     "M Charizard EX, XY Evolutions, full art secret rare. Raw, ungraded."),
    ("PC-013", "Espeon EX 117/122 XY BREAKpoint Full Art Pokemon", "LP", "7519", "7520", 35, 81,
     "Espeon EX 117/122, XY BREAKpoint, full art. Raw, ungraded. Corner wear -- see photos."),
    ("PC-014", "M Tyranitar EX 92/98 XY Ancient Origins Full Art Pokemon", "LP", "7521", "7522", 35, 75,
     "M Tyranitar EX 92/98, XY Ancient Origins, full art. Raw, ungraded. Minor wear -- see photos."),
    ("PC-015", "Misty's Determination 108/108 XY Evolutions Gold Full Art", "NM", "7523", "7524", 20, 18,
     "Misty's Determination 108/108, XY Evolutions, gold full art secret rare. Raw, ungraded."),
    ("PC-016", "Tyranitar EX 91/98 XY Ancient Origins Holo Pokemon", "NM", "7525", "7526", 15, 5,
     "Tyranitar EX 91/98, XY Ancient Origins. Raw, ungraded."),
    ("PC-017", "Charizard EX XY121 Black Star Promo Pokemon", "LP", "7527", "7528", 25, 32,
     "Charizard EX XY121, Black Star Promo. Raw, ungraded. Corner wear -- see photos."),
    ("PC-018", "Pikachu RC29 Generations Radiant Collection Full Art", "NM", "7531", "7532", 25, 32,
     "Pikachu RC29, Generations Radiant Collection, full art. Raw, ungraded."),
    ("PC-019", "Mewtwo EX XY107 Black Star Promo Pokemon", "LP", "7533", "7534", 15, 9,
     "Mewtwo EX XY107, Black Star Promo. Raw, ungraded. Corner whitening both top corners -- see photos."),
    ("PC-020", "Blastoise EX XY122 Black Star Promo Pokemon", "NM", "7535", "7536", 30, 59,
     "Blastoise EX XY122, Black Star Promo. Raw, ungraded."),
    ("PC-021", "Zapdos 29/83 XY Fates Collide Holo Pokemon", "NM", "7537", "7538", 15, 6,
     "Zapdos 29/83, XY Fates Collide. Raw, ungraded."),
    ("PC-022", "Alakazam EX 117/124 XY Fates Collide Full Art Pokemon", "LP", "7539", "7540", 15, 14,
     "Alakazam EX 117/124, XY Fates Collide, full art. Raw, ungraded. Corner wear -- see photos."),
    ("PC-023", "Pikachu EX XY124 Black Star Promo Pokemon", "LP", "7541", "7542", 25, 32,
     "Pikachu EX XY124, Black Star Promo. Raw, ungraded. Minor corner wear -- see photos."),
    ("PC-024", "M Manectric EX 24/119 XY Phantom Forces Full Art Pokemon", "NM", "7545", "7546", 15, 12,
     "M Manectric EX 24/119, XY Phantom Forces, full art. Raw, ungraded."),
    ("PC-025", "M Alakazam EX 118/124 XY Fates Collide Full Art Pokemon", "LP", "7547", "7548", 30, 55,
     "M Alakazam EX 118/124, XY Fates Collide, full art. Raw, ungraded. Corner wear -- see photos."),
    ("PC-026", "Leafeon EX 10/83 XY Fates Collide Holo Pokemon", "MP", "7549", "7550", 15, 9,
     "Leafeon EX 10/83, XY Fates Collide. Raw, ungraded. Visible crease and bent corner -- see photos, priced accordingly."),
    ("PC-027", "Wally 107/108 XY Roaring Skies Full Art Trainer Pokemon", "NM", "7551", "7552", 18, 16,
     "Wally 107/108, XY Roaring Skies, full art secret rare. Raw, ungraded."),
    ("PC-028", "Mewtwo EX 103/108 XY Evolutions Gold Full Art Pokemon", "NM", "7553", "7554", 26, 34,
     "Mewtwo EX 103/108, XY Evolutions, gold full art secret rare. Raw, ungraded."),
    ("PC-029", "Brock's Grit 107/108 XY Evolutions Gold Full Art Trainer", "NM", "7555", "7557", 15, 6,
     "Brock's Grit 107/108, XY Evolutions, gold full art. Raw, ungraded."),
]

rows = []
for sku, title, cond, front, back, start_gbp, nm_est_gbp in ((c[0], c[1], c[2], c[3], c[4], c[5], c[6]) for c in CARDS):
    pass  # placeholder to keep structure obvious; real loop below

rows = []
for sku, title, cond, front, back, start_gbp, nm_est_gbp, desc in CARDS:
    bin_gbp = max(round(2 * nm_est_gbp), 2 * start_gbp)
    rows.append({
        "sku": sku,
        "title": title,
        "card_condition": cond,
        "start_price_gbp": f"{start_gbp:.2f}",
        "buy_it_now_price_gbp": f"{bin_gbp:.2f}",
        "listing_duration": "DAYS_7",
        "description": desc,
        "image_urls": f"{IMG.format(front)}|{IMG.format(back)}",
    })

fieldnames = ["sku", "title", "card_condition", "start_price_gbp", "buy_it_now_price_gbp",
              "listing_duration", "description", "image_urls"]
with open("pokemon_cards.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=fieldnames)
    w.writeheader()
    w.writerows(rows)

print(f"{len(rows)} rows written to pokemon_cards.csv")
for r in rows:
    print(f"  {r['sku']:<8} start £{r['start_price_gbp']:<7} BIN £{r['buy_it_now_price_gbp']:<8} {r['title']}")
