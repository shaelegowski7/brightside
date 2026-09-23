"""One-time eBay setup helper. Not imported by the running app.

Everything the eBay Sell API needs before a single manual can be listed is
a one-off human step, and each one produces a value that goes in .env /
Railway. This drives them in order:

    python -m tools.ebay_consent check                     # what is set
    python -m tools.ebay_consent url                       # open, click Agree
    python -m tools.ebay_consent exchange "<code>"         # -> EBAY_REFRESH_TOKEN
    python -m tools.ebay_consent policies                  # -> the 3 policy IDs
    python -m tools.ebay_consent locations                 # -> EBAY_MERCHANT_LOCATION_KEY
    python -m tools.ebay_consent create-location KEY SW1A1AA
    python -m tools.ebay_consent categories "Haynes manual"   # -> categoryId

The consent step is the only one that needs a browser, and it must be done
once per environment (sandbox and production have separate keysets,
RuNames and refresh tokens -- a sandbox refresh token will not work in
production and vice versa).

See docs/EBAY_SETUP.md for what to do before any of this works.
"""
import json
import sys
from urllib.parse import unquote

from app import ebay_client
from app.config import get_ebay_settings


def _print_header(text: str) -> None:
    print(f"\n{text}\n" + "-" * len(text))


def cmd_check() -> int:
    s = get_ebay_settings()
    _print_header(f"eBay config ({s.env})")
    rows = [
        ("EBAY_ENV", s.env),
        ("EBAY_CLIENT_ID", s.client_id),
        ("EBAY_CLIENT_SECRET", s.client_secret),
        ("EBAY_RU_NAME", s.ru_name),
        ("EBAY_REFRESH_TOKEN", s.refresh_token),
        ("EBAY_MARKETPLACE_ID", s.marketplace_id),
        ("EBAY_MERCHANT_LOCATION_KEY", s.merchant_location_key),
        ("EBAY_FULFILLMENT_POLICY_ID", s.fulfillment_policy_id),
        ("EBAY_PAYMENT_POLICY_ID", s.payment_policy_id),
        ("EBAY_RETURN_POLICY_ID", s.return_policy_id),
    ]
    for name, value in rows:
        # Never print secrets back out -- this gets run in shared terminals.
        if not value:
            shown = "-- NOT SET --"
        elif name in ("EBAY_CLIENT_SECRET", "EBAY_REFRESH_TOKEN"):
            shown = f"set ({len(value)} chars)"
        else:
            shown = value
        print(f"  {name:<30} {shown}")

    print(f"\n  API base:     {ebay_client.api_base()}")
    print(f"  Consent host: {ebay_client.auth_base()}")
    print(f"\n  can call eBay:      {'yes' if ebay_client.is_configured() else 'no'}")
    missing = ebay_client.publishing_prerequisites_missing()
    print(f"  can publish offers: {'yes' if not missing else 'no -- unset: ' + ', '.join(missing)}")
    return 0


def cmd_url() -> int:
    s = get_ebay_settings()
    if not s.client_id or not s.ru_name:
        print("EBAY_CLIENT_ID and EBAY_RU_NAME must be set first (docs/EBAY_SETUP.md steps 2-3).")
        return 1
    _print_header("Open this in a browser, sign in, click 'I agree'")
    print(ebay_client.consent_url())
    print(
        "\n*** Sign in as whichever eBay account should own the listings this\n"
        "    run creates -- Brightside Commerce, a personal account, whatever\n"
        "    you intend right now. The token binds to whoever clicks Agree,\n"
        "    NOT to the developer account, so consenting as the wrong user\n"
        "    silently sends every listing to that user's shop. The URL sends\n"
        "    prompt=login to force a fresh sign-in, but check the account\n"
        "    name on the page anyway before clicking Agree.\n"
        "\neBay then redirects to your RuName's accept URL with ?code=... in the\n"
        "query string. Copy that code value and run:\n\n"
        '    python -m tools.ebay_consent exchange "<code>"\n\n'
        "The code expires in 5 minutes, so do it straight away."
    )
    return 0


def cmd_exchange(code: str) -> int:
    # Codes arrive URL-encoded in the redirect; pasting them raw is the
    # most common reason exchange fails with invalid_grant.
    data = ebay_client.exchange_code_for_tokens(unquote(code))
    if not data:
        print("Exchange failed -- see the [EBAY] line above for eBay's own reason.")
        print("  invalid_grant  -> code expired (5 min) or already used; get a fresh one")
        print("  invalid_client -> EBAY_CLIENT_ID/SECRET wrong, or wrong environment")
        print("  invalid_request-> EBAY_RU_NAME is not the RuName eBay generated")
        return 1

    refresh = data.get("refresh_token")
    if not refresh:
        print(f"No refresh_token in response: {json.dumps(data)[:400]}")
        return 1

    _print_header("Success -- add this to .env (and Railway)")
    print(f"EBAY_REFRESH_TOKEN={refresh}")
    expires = data.get("refresh_token_expires_in")
    if expires:
        print(f"\n(valid for {int(expires) // 86400} days -- re-run this flow before then)")
    return 0


def cmd_policies() -> int:
    policies = ebay_client.list_business_policies()
    if not any(policies.values()):
        print(
            "No business policies returned. Either the seller account has not opted in to\n"
            "Business Policies yet (docs/EBAY_SETUP.md step 6), or the refresh token is\n"
            "missing the sell.account scope -- re-run the consent flow if you changed SCOPES."
        )
        return 1

    env_names = {
        "fulfillment": "EBAY_FULFILLMENT_POLICY_ID",
        "payment": "EBAY_PAYMENT_POLICY_ID",
        "return": "EBAY_RETURN_POLICY_ID",
    }
    for kind, items in policies.items():
        _print_header(f"{kind} policies  ->  {env_names[kind]}")
        if not items:
            print("  (none -- create one in eBay > Account > Business policies)")
        for item in items:
            policy_id = item.get(f"{kind}PolicyId") or item.get("policyId") or "?"
            print(f"  {policy_id}   {item.get('name', '')}")
    return 0


def cmd_locations() -> int:
    locations = ebay_client.list_inventory_locations()
    _print_header("Inventory locations  ->  EBAY_MERCHANT_LOCATION_KEY")
    if not locations:
        print("  (none -- create one with: create-location <KEY> <POSTCODE>)")
        return 0
    for loc in locations:
        address = (loc.get("location") or {}).get("address") or {}
        print(f"  {loc.get('merchantLocationKey', '?'):<20} "
              f"{loc.get('name', '')}  {address.get('postalCode', '')}  "
              f"[{loc.get('merchantLocationStatus', '')}]")
    return 0


def cmd_create_location(key: str, postcode: str) -> int:
    if ebay_client.create_inventory_location(key, postcode):
        print(f"Created. Set EBAY_MERCHANT_LOCATION_KEY={key}")
        return 0
    print("Failed -- see the [EBAY] line above.")
    return 1


def cmd_categories(query: str) -> int:
    suggestions = ebay_client.suggest_categories(query)
    if not suggestions:
        print("No suggestions returned (check the token has the base api_scope).")
        return 1
    _print_header(f"Category suggestions for {query!r}  ->  category_id column / --category")
    for s in suggestions:
        print(f"  {s['categoryId']:<10} {s['path']}")
    print("\nThe first is eBay's best guess. Sanity-check it against a live listing of the\n"
          "same product before listing 100 of them.")
    return 0


_USAGE = """usage: python -m tools.ebay_consent <command>

  check                          show which settings are present
  url                            print the browser consent URL
  exchange <code>                swap the consent code for a refresh token
  policies                       list business policy IDs
  locations                      list inventory locations
  create-location <KEY> <POSTCODE>
  categories <query>             suggest an eBay categoryId
"""


def main(argv: list[str]) -> int:
    if not argv:
        print(_USAGE)
        return 2
    command, args = argv[0], argv[1:]

    needs_creds = command not in ("check", "url")
    if needs_creds and not ebay_client.is_configured():
        print("eBay credentials are not set -- run `check` first, and see docs/EBAY_SETUP.md.")
        return 1

    if command == "check":
        return cmd_check()
    if command == "url":
        return cmd_url()
    if command == "exchange":
        if len(args) != 1:
            print("usage: exchange <code>")
            return 2
        return cmd_exchange(args[0])
    if command == "policies":
        return cmd_policies()
    if command == "locations":
        return cmd_locations()
    if command == "create-location":
        if len(args) != 2:
            print("usage: create-location <KEY> <POSTCODE>")
            return 2
        return cmd_create_location(args[0], args[1])
    if command == "categories":
        if len(args) != 1:
            print('usage: categories "<query>"')
            return 2
        return cmd_categories(args[0])

    print(_USAGE)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
