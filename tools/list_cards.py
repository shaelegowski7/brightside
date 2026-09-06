"""Lists a CSV of raw trading cards onto eBay as AUCTION listings. The
front end for app/ebay_cards.py -- sibling to tools/list_manuals.py, not
an extension of it (see app/ebay_cards.py's module docstring for why).

    # always do this first -- validates every row, sends nothing
    python -m tools.list_cards pokemon_cards.csv --dry-run

    # then, once the sheet is clean
    python -m tools.list_cards pokemon_cards.csv

    # create the offers but leave them unpublished, to eyeball in Seller Hub
    python -m tools.list_cards pokemon_cards.csv --no-publish

Safe to re-run: progress is recorded per SKU in the ebay_listings table, so
a second run skips what already went live and resumes anything that stopped
part-way rather than creating duplicate offers.
"""
import argparse
import sys

from app import ebay_client, ebay_cards


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="list_cards", description=__doc__)
    parser.add_argument("csv_path", help="CSV of cards to list")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate and build every payload without calling eBay")
    parser.add_argument("--no-publish", dest="publish", action="store_false",
                        help="create offers but leave them unpublished")
    args = parser.parse_args(argv)

    rows, problems = ebay_cards.load_csv(args.csv_path)
    if problems:
        print(f"{len(problems)} row(s) rejected before sending:")
        for problem in problems:
            print(f"  - {problem}")
    if not rows:
        print("Nothing valid to list.")
        return 1
    print(f"{len(rows)} row(s) parsed OK.")

    if not args.dry_run and not ebay_client.is_configured():
        print("eBay credentials are not set -- see docs/EBAY_SETUP.md, "
              "or re-run with --dry-run to check the sheet offline.")
        return 1

    if not args.dry_run:
        env = "PRODUCTION" if ebay_client.api_base() == "https://api.ebay.com" else "sandbox"
        action = "publish live auctions" if args.publish else "create unpublished offers"
        print(f"\nAbout to {action} for {len(rows)} item(s) on {env}.")
        if env == "PRODUCTION":
            reply = input("Type 'yes' to continue: ").strip().lower()
            if reply != "yes":
                print("Aborted.")
                return 1

    if args.dry_run:
        result = ebay_cards.list_cards(None, rows, publish=args.publish, dry_run=True)
    else:
        from app.database import SessionLocal

        db = SessionLocal()
        try:
            result = ebay_cards.list_cards(db, rows, publish=args.publish)
        finally:
            db.close()

    print("\n" + result.summary())
    for note in result.notes:
        print(f"  - {note}")
    for problem in result.problems:
        print(f"  ! {problem}")
    if result.failed:
        print(f"\nFailed SKUs ({len(result.failed)}) -- reasons are in ebay_listings.last_error:")
        for sku in result.failed[:20]:
            print(f"  - {sku}")
        if len(result.failed) > 20:
            print(f"  ... and {len(result.failed) - 20} more")
    return 0 if not result.failed else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
