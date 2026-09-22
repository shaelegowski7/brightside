# eBay Sell API setup

Everything needed to get from "no eBay developer account" to "100 manuals
live on the Brightside Commerce shop". Each step produces a value that goes
into `.env` (and Railway, if the app should list from there too).

Work through it in order — later steps genuinely cannot be done before
earlier ones. `python -m tools.ebay_consent check` tells you where you are
at any point, and works before anything is configured.

> **We are going straight to production, using the lister's own safety
> flags rather than eBay's sandbox.** `--dry-run` makes no API calls at
> all, and `--no-publish` creates offers that sit unpublished until you
> approve them in Seller Hub. eBay's Sell sandbox needs its own test users,
> its business-policy opt-in is often unavailable, and its taxonomy differs
> from production -- so a clean sandbox run does not prove much about the
> real one.
>
> If you do want sandbox: it is completely separate -- different keyset,
> RuName, refresh token and policies, none of which carry over. `EBAY_ENV`
> picks between them and is the only setting here that is not a secret.

---

## 1. Developer account

Register at <https://developer.ebay.com/> using the same eBay account that
owns the Brightside Commerce shop. It is free and approval is usually
immediate — unlike Amazon's SP-API application, there is no review process
for the Sell APIs.

> **If you have more than one eBay account, two different accounts are in
> play and only one decides where listings land.** The *developer* account
> owns the keyset; the *seller* account is whoever clicks "I agree" in step
> 4, and that is what the refresh token binds to. They do not have to
> match. Registering the developer account under Brightside keeps ownership
> tidy, but step 5 is the one that actually matters -- get that wrong and
> the manuals list on your personal account with no error anywhere.

## 2. Application keyset

Developer portal → **Application Keys**. You get two keysets, Sandbox and
Production. Each has three values; two of them are what we need:

| eBay's name | Ours |
|---|---|
| App ID (Client ID) | `EBAY_CLIENT_ID` |
| Cert ID (Client Secret) | `EBAY_CLIENT_SECRET` |
| Dev ID | not used by the REST APIs |

Take these from the **Production** keyset.

```
EBAY_ENV=production
EBAY_CLIENT_ID=...
EBAY_CLIENT_SECRET=...
```

## 3. RuName (redirect URL name)

On the same page, under the keyset: **User Tokens** → *Get a Token from eBay
via Your Application* → add a redirect URL if there isn't one.

Fill in the "Your auth accept URL" / "Your auth decline URL" fields. These
can be any page you control — nothing is served from them by this repo, and
you will be reading the `code` out of the address bar by hand. The landing
site is fine: `https://brightsidecommerce.co.uk/`.

eBay then generates a **RuName**, which looks like
`Shae_Legowski-ShaeLego-bright-abcdefgh` — *not* a URL.

```
EBAY_RU_NAME=Shae_Legowski-ShaeLego-bright-abcdefgh
```

> This is the single most common thing to get wrong. eBay's OAuth
> `redirect_uri` parameter takes the RuName string, not the accept URL. Put
> a real `https://…` in there and the consent step fails with
> `invalid_request`.

## 4. Marketplace account deletion endpoint

eBay will not activate a production keyset until the application either
subscribes to marketplace account deletion/closure notifications or is
formally exempted from them. Their words: *"New third-party developers must
subscribe to or opt out ... before they make their first production API
call. Once ... subscribed ... or they have successfully opted out, the
keyset/App ID is activated."* Skip this and step 5 fails with no obvious
reason why.

We subscribe rather than opt out. The opt-out is a standing declaration that
we persist no eBay data -- eBay penalises accounts that declare it wrongly --
and it would stop being true the first time Brightside reads an order and
sees a buyer's name and address.

The endpoint is `landing/api/ebay-deletion.js`, a dependency-free Vercel
function on the landing site, which is the only part of Brightside with a
public HTTPS address. It answers eBay's `GET ?challenge_code=` handshake and
acknowledges the `POST` notifications. Run its tests with:

```bash
node tests/ebay_deletion_endpoint.test.js
```

To deploy it:

1. Add `EBAY_VERIFICATION_TOKEN` to the **landing** Vercel project (Settings
   -> Environment Variables, all environments). The value is in `.env`; it
   must be 32-80 characters of `[A-Za-z0-9_-]` and nothing else.
2. Deploy, then confirm the handshake answers before touching the eBay form:

   ```bash
   curl -s "https://www.brightsidecommerce.co.uk/api/ebay-deletion?challenge_code=test123"
   ```

   A `challengeResponse` hex string means ready. A 500 means the env var did
   not reach the deployment; a 404 means the function did not deploy at all.
3. In the developer portal, **Event Notification Delivery Method** ->
   **Marketplace Account Deletion**: leave the exemption toggle **off**, set
   the alert email, set the endpoint to
   `https://www.brightsidecommerce.co.uk/api/ebay-deletion`, paste the same
   verification token, and Save. eBay sends the challenge the instant you
   save.

> **The endpoint URL is part of the hash**, alongside the challenge code and
> the verification token, in that order. So the URL registered with eBay must
> match `ENDPOINT` in the function *character for character* -- `www` and a
> trailing slash both matter. The apex domain 308-redirects to `www`, so
> register the `www` form. If you ever move the endpoint, change both sides
> together or validation fails with nothing to show why.

## 5. Grant consent (one time, needs a browser)

```bash
python -m tools.ebay_consent url
```

Open the printed URL, **sign in as Brightside Commerce**, click **I agree**.
eBay redirects to your accept URL with `?code=v%5E1.1%23...` in the query
string.

> The consent URL sends `prompt=login`, which forces a fresh sign-in rather
> than reusing whatever eBay session the browser already holds -- otherwise
> an already-signed-in personal account gets consented silently. Check the
> account name shown on the consent page before clicking Agree; if it is
> wrong, sign out (or use a private window) and re-run `url`.
>
> Consented as the wrong account? Revoke it at eBay -> Account settings ->
> Site preferences -> **Third-party application access**, then redo this
> step. Anything already listed has to be withdrawn from that account by
> hand.

Copy that `code` value and, **within 5 minutes** (the code is one-shot and
expires fast):

```bash
python -m tools.ebay_consent exchange "<code>"
```

It prints the refresh token. That token is good for about 18 months; put it
in `.env` and diarise the renewal.

```
EBAY_REFRESH_TOKEN=v^1.1#i^1#...
```

From here on the app refreshes its own 2-hour access tokens; no more
browser steps until the refresh token expires.

Check it worked:

```bash
python -m tools.ebay_consent check     # "can call eBay: yes"
```

## 6. Business policies, and a location

An offer cannot be published without a payment policy, a return policy, a
fulfilment (postage) policy, **and** an inventory location. Four separate
IDs, all one-time setup.

First, opt the seller account in to Business Policies — eBay account
settings → **Business policies** → opt in — and create one of each. This is
done in eBay's own UI, not here.

Then read the IDs back:

```bash
python -m tools.ebay_consent policies
```

```
EBAY_FULFILLMENT_POLICY_ID=...
EBAY_PAYMENT_POLICY_ID=...
EBAY_RETURN_POLICY_ID=...
```

And the location — a postcode is enough for a home/office seller:

```bash
python -m tools.ebay_consent locations                     # list existing
python -m tools.ebay_consent create-location HOME SW1A1AA  # or create one
```

```
EBAY_MERCHANT_LOCATION_KEY=HOME
```

`check` should now say **can publish offers: yes**.

## 7. A category ID

Every offer needs one. Ask eBay rather than guessing:

```bash
python -m tools.ebay_consent categories "Haynes manual Ford Fiesta"
```

Haynes manuals sit under Vehicle Parts & Accessories, **not** Books. Pass
the ID as `--category` for the whole run, or per row in the `category_id`
column when the batch is mixed.

The Brightside Books batch uses **183721** -- eBay UK's "Car Service &
Repair Manuals" -- baked into `tools/_build_brightside_books.py`. That was
derived from eBay's public browse URLs, which take the form
`/b/<Name>/<categoryId>/bn_<facet>`; the year and literature-type views all
share 183721, so it is a leaf category, which is what a listing requires.

> It has **not** been confirmed against the Taxonomy API. Run the command
> above once the keyset lands and check it agrees before publishing -- it
> is free and takes seconds, and a wrong category misfiles the whole batch.
> You can also verify by hand: open a live Haynes listing on ebay.co.uk,
> click through to its category, and read the number out of the URL.

## 8. Host the photos

**This is the step that blocks people.** The Inventory API takes image
*URLs* — it will not accept an upload, and it cannot read a local file. Every
URL must already be publicly reachable over HTTPS before you run the lister;
`--dry-run` rejects anything that isn't, rather than letting a batch die
halfway.

Anywhere public works (S3, Cloudflare R2, the landing site's own hosting).
eBay's own picture hosting is only reachable through the legacy Trading API,
which this repo deliberately does not depend on.

---

## Listing the manuals

Fill in a CSV using `haynes_manuals_template.csv` as the shape. Columns:

| Column | Required | Notes |
|---|---|---|
| `sku` | yes | Your own identifier; the resume key. Must be unique. |
| `condition` | yes | `USED_GOOD` etc., or plain words — "very good", "acceptable" |
| `price_gbp` | yes | `8.99` or `£8.99` |
| `title` | no | Built from make/model/years if blank. 80 char limit. |
| `make`, `model`, `year_from`, `year_to` | no | Become eBay item specifics |
| `haynes_number` | no | Sent as MPN |
| `isbn` | no | Lets eBay match its own catalogue and auto-fill specifics |
| `quantity` | no | Defaults to 1 |
| `category_id` | no | Overrides `--category` for that row |
| `condition_description` | no | Free text, used-condition only |
| `image_urls` | no | Pipe-separated, `https://` only |

Then:

```bash
# 1. always first -- validates all 100 rows, sends nothing, needs no DB
python -m tools.list_manuals haynes_manuals.csv --dry-run

# 2. create the offers but leave them dark; review them in Seller Hub
python -m tools.list_manuals haynes_manuals.csv --category 183721 --no-publish

# 3. once they look right, publish
python -m tools.list_manuals haynes_manuals.csv --category 183721
```

Step 2 is the real safety net on production: the offers exist and are fully
formed, but no listing is live and nothing is charged. Check a few in Seller
Hub, then run step 3 -- it resumes from the offers already created rather
than making new ones.

Re-running is safe. Progress is recorded per SKU in the `ebay_listings`
table, so a second run skips what already published and resumes anything
that stopped part-way. That matters: `bulkCreateOffer` is **not** idempotent
— calling it twice for one SKU is an error from eBay, not a no-op — so the
resume path is the only correct way to retry a partial batch.

---

## Things that will bite

- **eBay returns HTTP 200 for a batch containing failures.** Per-item
  `statusCode` is the real verdict. The client handles this; anything new
  built on top must too.
- **Selling limits.** eBay caps how much a seller account can list per
  month, and the cap is low on newer accounts. 100 items in one go may hit
  it. Check Seller Hub → Monthly limits first; request an increase if
  needed. Nothing in this repo can work around it.
- **Bulk endpoints cap at 25 per call.** Handled by chunking — 100 manuals
  is 4 calls per stage — but relevant if you are reading rate limits.
- **The docs site blocks scripts.** `developer.ebay.com` returns 403 to
  curl and anything non-browser. The mirror at `www.edp.ebay.com` serves the
  same pages and does not. Useful when re-checking a contract.
- **Renewal.** The refresh token dies at ~18 months. When calls start
  failing with `invalid_grant`, redo step 5 — nothing else changes.

## Environment variables

```
EBAY_ENV=production               # or sandbox
EBAY_CLIENT_ID=
EBAY_CLIENT_SECRET=
EBAY_RU_NAME=
EBAY_REFRESH_TOKEN=
EBAY_MARKETPLACE_ID=EBAY_GB       # default
EBAY_MERCHANT_LOCATION_KEY=
EBAY_FULFILLMENT_POLICY_ID=
EBAY_PAYMENT_POLICY_ID=
EBAY_RETURN_POLICY_ID=
```

`EBAY_VERIFICATION_TOKEN` and `EBAY_DELETION_ENDPOINT` are the odd ones out:
they belong to the **landing Vercel project**, not the backend, and nothing
in `app/` reads them. They are kept in `.env` too so the value you pasted
into eBay's portal is written down somewhere other than eBay's portal.

All default to empty, and `ebay_client.is_configured()` is the single
on/off switch — an unconfigured deploy simply never calls eBay, exactly
like `spapi_client.py`. These deliberately live in their own `EbaySettings`
rather than the app-wide `Settings`, so the setup tools run on a machine
where `DATABASE_URL` and the Supabase secrets are not configured.
