"""Check that each supplier lead on the sourcing shortlist actually lists the
product: search the supplier's own site for the EAN, then the product name.

    python tools/verify_leads.py <rows.json> <out.json>

<rows.json> is the merged shortlist (source_batch.py rows). <out.json> maps
"<asin>|<lead url or name>" to {"status", "how", "url"} and is reused on
rerun, so an interrupted run picks up where it stopped.

Statuses:
  listed      the EAN, or the product's distinctive name words, appear in
              the site's own search results (query echo stripped first)
  not_found   the site searched fine and the product isn't there
  unchecked   no URL, the site blocks automated access, needs a login, or
              has no search we can find

Direct requests, not ScraperAPI: a ScraperAPI Google search costs 25
credits and there are ~6,300 lead/product pairs against ~31k credits left
(2026-10-11). "not_found" means the public search didn't show it, not proof
the supplier can't get it -- trade sites often hide lines until login.

Name matching was tuned on a sample (2026-10-11): words must sit near the
brand name (one product card, not anywhere on a results page -- Vending
Superstore "matched" Lavazza Qualita Rossa off "1kg coffee beans" spread
across other products), units are normalised (Casdron writes "1 Litre",
Keepa "1Ltr"), and pack sizes are a bonus rather than required (Science in
Sport's own search doesn't show "60ml").
"""
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import requests

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/129.0 Safari/537.36",
       "Accept-Language": "en-GB,en;q=0.9"}
_TIMEOUT = 12
_WORKERS = 48
_NO_RESULTS = re.compile(r"no (products|results) (were )?found|0 results|nothing (was )?found|"
                         r"did not match any|no results for|sorry, no|returned no results", re.I)
_STOP = {"the", "and", "for", "with", "pack", "of", "x", "in", "a", "to", "new", "uk", "by", "from", "set",
         "size", "free", "men", "women", "unisex", "edition", "original", "eau", "de", "spray"}
_SKIP_DOMAINS = {"amazon.co.uk", "amazon.com", "ebay.co.uk", "ebay.com", "alibaba.com", "dhgate.com",
                 "scribd.com", "mapquest.com", "linkedin.com", "facebook.com", "youtube.com", "wikipedia.org"}
_SIZE = re.compile(r"^\d+(\.\d+)?(ml|g|kg|l|cm|mm|m|pcs|pack|w|v|mah|oz|ct)$")
# Product-type and colour words: present on half a results page, so they
# can't prove which product a page lists (Lavazza "coffee beans").
_GENERIC = {"coffee", "beans", "tea", "oil", "cream", "gel", "gels", "spray", "water", "black", "white", "clear",
            "red", "blue", "green", "pink", "orange", "yellow", "grey", "silver", "gold", "high", "large", "small",
            "mini", "medium", "pro", "plus", "max", "ultra", "premium", "natural", "classic", "refill", "bottle",
            "tube", "box", "bag", "case", "cover", "kit", "tool", "tools", "toy", "game", "book", "card", "cards",
            "wash", "cleaner", "shampoo", "soap", "lotion", "serum", "toner", "brush", "lead", "cable", "charger",
            "light", "lamp", "battery", "batteries", "food", "dog", "cat", "adult", "kids", "baby", "home", "garden",
            # fragrance words: every perfume wholesaler's results page is full of them
            "parfum", "parfums", "perfume", "perfumes", "toilette", "cologne", "edp", "edt",
            "games", "products", "product", "accessories"}


def _domain(url: str | None) -> str | None:
    if not url:
        return None
    d = urlparse(url if "//" in url else "https://" + url).netloc.lower()
    return d[4:] if d.startswith("www.") else d or None


def _text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", html.replace("&amp;", "&").replace("&nbsp;", " "))


def _norm(s: str) -> str:
    s = s.lower().replace("’", "'")
    s = re.sub(r"(\d)\s*(litres?|liters?|ltrs?)\b", r"\1l", s)
    return re.sub(r"(\d)\s+(ml|g|kg|l|cm|mm|pcs|pack|oz)\b", r"\1\2", s)


def _strong_model(w: str) -> bool:
    """3028 or ABC123 is a model number; EAS1 or 4K is too weak to insist on."""
    digits = sum(c.isdigit() for c in w)
    return (w.isdigit() and len(w) >= 3) or (digits >= 2 and len(w) - digits >= 2)


def _tokens(title: str, brand: str | None) -> dict:
    """A strong model number (must match), distinctive words (half must
    match) and generic words (count towards the 2-hit minimum only). Brand
    words are left out: the brand is the anchor."""
    brand_words = set(re.findall(r"[a-z0-9]+", (brand or "").lower()))
    raw = [w for w in re.findall(r"[a-z0-9]+", _norm(title))
           if w not in _STOP and w not in brand_words and len(w) > 2]
    model = [w for w in raw if any(c.isdigit() for c in w) and not _SIZE.match(w) and _strong_model(w)]
    rest = list(dict.fromkeys(w for w in raw if not _SIZE.match(w) and w not in model))
    return {"model": model[:1], "words": [w for w in rest if w not in _GENERIC][:4],
            "generic": [w for w in rest if w in _GENERIC][:3]}


def _has(text: str, w: str) -> bool:
    stem = w[:-1] if len(w) > 4 and w.endswith("s") else w
    return re.search(rf"\b{re.escape(stem)}s?\b", text) is not None


def _flat(s: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", _norm(s)))


def _verdict(text: str, ean: str | None, tokens: dict | None, brand: str | None,
             brand_site: bool = False, query: str | None = None) -> tuple[str, str]:
    if _NO_RESULTS.search(text[:5000]) and not (ean and ean in text):
        return "not_found", "search returned nothing"
    if ean:
        return ("listed", "barcode") if (ean in text or ean.lstrip("0") in text) else ("not_found", "barcode not in results")
    t = _norm(text)
    if query:
        # Results pages echo the search back in headings and breadcrumbs, in
        # their own case and punctuation ("You searched for: BROADMAN CHURCH
        # SUPPLIES 100 fellowship cup" made communion cups a hair-and-beauty
        # "match", 2026-10-11). Compare on bare words.
        t = _flat(t).replace(_flat(query), " ")
    anchor = (re.findall(r"[a-z0-9]{3,}", (brand or "").lower()) or [None])[0]
    spots = [m.start() for m in re.finditer(re.escape(anchor), t)] if anchor else []
    if anchor and not spots and not brand_site:
        return "not_found", "brand not in results"
    if spots and not brand_site and len(spots) <= 80:
        windows = [t[max(0, p - 200):p + 300] for p in spots]
    else:
        # A brand's own site (or a page that is all one brand): the brand is
        # everywhere, so it can't anchor anything. Slide over the whole page.
        windows = [t[i:i + 500] for i in range(0, max(len(t) - 250, 1), 250)]
    words, generic, model = tokens["words"], tokens["generic"], tokens["model"]
    need = -(-len(words) // 2)
    best = 0
    for w in windows:
        if model and not _has(w, model[0]):
            continue
        hit_words = [x for x in words if _has(w, x)]
        hits = model + hit_words + [x for x in generic if _has(w, x)]
        best = max(best, len(hit_words))
        # The first distinctive word is the product's own name ("Worldwide"
        # in "51 Worldwide Games"); without it, it's a different product.
        if words and words[0] not in hit_words:
            continue
        if len(hit_words) >= need and len(hits) >= 2:
            return "listed", "name: " + " ".join(hits)
    return "not_found", f"name: {best}/{need} distinctive words"


class Site:
    """One supplier domain: platform detection once, then cached searches."""

    def __init__(self, domain: str):
        self.domain = domain
        self.base = f"https://{domain}"
        self.platform = None
        self.blocked = None
        self.form = None
        self.throttled = False
        self.home = ""
        self.cache: dict[str, tuple] = {}
        self.s = requests.Session()
        self.s.headers.update(_UA)

    def _get(self, url: str) -> requests.Response | None:
        """One request a second per site, one short back-off on 429, then
        give the site up for this run. Shopify shops throttle hard; waiting
        20-60s per retry stalled the whole run on a few sites (2026-10-11),
        so a throttled site is reported as rate-limited and retried on the
        next run instead."""
        if self.throttled:
            return None
        for attempt in range(2):
            time.sleep(1.0)
            try:
                # requests' timeout is per read, so a server trickling bytes
                # never trips it -- the 2026-10-11 run hung 48 workers that
                # way. Hard cap on total time and size instead.
                r = self.s.get(url, timeout=_TIMEOUT, allow_redirects=True, stream=True)
                start, chunks, size = time.time(), [], 0
                for chunk in r.iter_content(65536):
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > 3_000_000 or time.time() - start > 20:
                        break
                r._content, r._content_consumed = b"".join(chunks), True
                r.close()
            except requests.RequestException:
                return None
            if r.status_code != 429:
                return r
            if attempt == 0:
                wait = r.headers.get("Retry-After", "")
                time.sleep(min(int(wait), 15) if wait.isdigit() else 10)
        self.throttled = True
        return None

    def detect(self) -> None:
        r = self._get(self.base)
        if r is None or r.status_code in (401, 403, 503) or "cf-chl" in r.text[:20000]:
            self.blocked = ("rate-limited, try later" if self.throttled else
                            f"site blocks checks ({r.status_code if r is not None else 'unreachable'})")
            return
        h = r.text[:300000].lower()
        self.form = self._search_form(r.text[:300000], r.url)
        self.home = _text(r.text)[:4000]
        if "cdn.shopify.com" in h or "shopify.theme" in h:
            self.platform = "shopify"
        elif "woocommerce" in h or "wp-content" in h:
            self.platform = "woocommerce"
        elif "magento" in h or "mage/cookies" in h or "/static/version" in h:   # not "mage/": matches "image/"
            self.platform = "magento"
        elif "bigcommerce" in h:
            self.platform = "bigcommerce"
        else:
            self.platform = "generic"

    @staticmethod
    def _search_form(html: str, page_url: str) -> str | None:
        """The site's own search box, as a URL template ending in '<param>='.
        Bespoke trade sites rarely sit on a standard search path; 452 of
        the first 1,292 checks were 'search 404' before this (2026-10-11)."""
        from urllib.parse import urljoin
        for m in re.finditer(r"(?is)<form\b([^>]*)>(.*?)</form>", html):
            attrs, body = m.group(1), m.group(2)
            if re.search(r"method\s*=\s*['\"]?post", attrs, re.I):
                continue
            inp = None
            for i in re.finditer(r"(?is)<input\b([^>]*)>", body):
                a = i.group(1)
                name = re.search(r"name\s*=\s*['\"]([^'\"]+)", a)
                if not name:
                    continue
                if re.search(r"type\s*=\s*['\"]?search", a, re.I) or name.group(1).lower() in (
                        "q", "s", "search", "query", "keyword", "keywords", "search_query", "term", "k", "searchterm"):
                    inp = name.group(1)
                    break
            if inp:
                action = re.search(r"action\s*=\s*['\"]([^'\"]*)", attrs)
                url = urljoin(page_url, action.group(1) if action and action.group(1).strip() else page_url)
                return url + ("&" if "?" in url else "?") + inp + "="
        return None

    def search(self, query: str) -> tuple[str | None, str | None]:
        """(results text with the query echo removed, url) or (None, reason)."""
        if query not in self.cache:
            self.cache[query] = self._search(query)
        return self.cache[query]

    def _search(self, query: str) -> tuple[str | None, str | None]:
        q = quote_plus(query)
        b = self.base
        urls = {
            "shopify": [f"{b}/search/suggest.json?q={q}&resources[type]=product&resources[limit]=10",
                        f"{b}/search?q={q}&type=product"],
            "woocommerce": [f"{b}/wp-json/wc/store/v1/products?search={q}&per_page=20", f"{b}/?s={q}&post_type=product"],
            "magento": [f"{b}/catalogsearch/result/?q={q}"],
            "bigcommerce": [f"{b}/search.php?search_query={q}"],
            "generic": [f"{b}/search?q={q}", f"{b}/?s={q}", f"{b}/search?query={q}",
                        f"{b}/catalogsearch/result/?q={q}", f"{b}/search.php?search_query={q}",
                        f"{b}/products?search={q}"],
        }[self.platform]
        if self.form and self.platform not in ("shopify",):
            urls = urls[:1] + [self.form + q] + urls[1:] if self.platform == "woocommerce" else [self.form + q] + urls
        last = "no search page found"
        for u in urls:
            r = self._get(u)
            if r is None:
                if self.throttled:
                    return None, "rate-limited, try later"
                last = "unreachable"
                continue
            if r.status_code != 200:
                last = f"search {r.status_code}"
                continue
            if "login" in urlparse(r.url).path.lower():
                return None, "needs login"
            if "/wc/store/" in u:
                try:
                    prods = r.json()
                    if not isinstance(prods, list):
                        continue
                    return " | ".join(f"{p.get('name', '')} {p.get('sku', '')} {_text(p.get('short_description') or '')} "
                                      f"{_text(p.get('description') or '')[:1500]}" for p in prods) or "0 results", u
                except ValueError:
                    continue
            if "suggest.json" in u:
                try:
                    prods = r.json()["resources"]["results"]["products"]
                    return " | ".join(f"{p.get('title', '')} {p.get('handle', '')}" for p in prods) or "0 results", u
                except (ValueError, KeyError, TypeError):
                    continue
            text = _text(r.text)
            if self.home and text[:4000] == self.home:
                last = "search ignored (returns the homepage)"
                continue
            text = re.sub(re.escape(query), " ", text, flags=re.I)
            return text, u
        return None, last


def main(rows_path: Path, out_path: Path) -> None:
    rows = json.loads(rows_path.read_text(encoding="utf-8"))
    results = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}
    lock = threading.Lock()

    by_domain: dict[str, list[tuple[dict, dict, str]]] = {}
    for r in rows:
        for l in r.get("leads") or []:
            key = f"{r['asin']}|{l.get('url') or l['name']}"
            if l["kind"] == "stocks_it":
                results[key] = {"status": "listed", "how": "in their scanned price list", "url": None}
                continue
            if key in results:
                continue
            d = _domain(l.get("url"))
            if not d or any(d == s or d.endswith("." + s) for s in _SKIP_DOMAINS):
                results[key] = {"status": "unchecked", "url": None,
                                "how": "no website on file" if not d else "marketplace, not a supplier"}
                continue
            by_domain.setdefault(d, []).append((r, l, key))
    todo = sum(len(v) for v in by_domain.values())
    print(f"[VERIFY] {todo} lead/product pairs on {len(by_domain)} sites to check", flush=True)
    done, last_save = [0], [time.time()]

    def save():
        out_path.write_text(json.dumps(results, indent=0), encoding="utf-8")

    def check_site(domain: str, pairs: list) -> None:
        site = Site(domain)
        site.detect()
        local = {}
        for r, l, key in pairs:
            if site.blocked or site.throttled:
                local[key] = {"status": "unchecked", "how": site.blocked or "rate-limited, try later", "url": None}
                continue
            eans = [e for e in (r.get("eans") or []) if e and e.isdigit()][:1]
            tokens = _tokens(r.get("keepa_title") or r.get("title") or "", r.get("brand"))
            verdict = None
            for e in eans:
                text, how = site.search(e)
                if text is None:
                    verdict = {"status": "unchecked", "how": how, "url": None}
                    break
                if _verdict(text, e, None, None)[0] == "listed":
                    verdict = {"status": "listed", "how": "barcode", "url": how}
                    break
            if verdict is None or verdict["status"] != "listed" and verdict["how"] != "needs login":
                brand = r.get("brand") or ""
                brand_site = bool(brand) and re.sub(r"[^a-z0-9]", "", brand.lower())[:6] in domain.replace(".", "")
                # Narrow query first; then the brand alone, whose results page
                # usually lists the whole range (site searches are often a
                # strict AND that a single extra word empties).
                title = r.get("keepa_title") or r.get("title") or ""
                after = re.sub(rf"(?i)^\s*{re.escape(brand)}\b", "", title) if brand else title
                lead_words = [w for w in re.findall(r"[A-Za-z][A-Za-z'-]+", after) if w.lower() not in _STOP][:3]
                queries = [" ".join([brand] + lead_words).strip(),
                           " ".join([brand] + tokens["model"] + tokens["words"][:2]).strip()]
                if brand_site:
                    # A brand's own shop names products without the brand
                    # ("TopOil", not "Osmo TopOil High Solid").
                    queries += lead_words[:1] + tokens["model"][:1]
                elif brand and not brand_site:
                    queries.append(brand)
                queries = list(dict.fromkeys(queries))[:3]
                for name_q in [q for q in queries if q]:
                    text, how = site.search(name_q)
                    if text is None:
                        verdict = verdict or {"status": "unchecked", "how": how, "url": None}
                        continue
                    status, why = _verdict(text, None, tokens, brand, brand_site, name_q)
                    verdict = {"status": status, "how": why, "url": how}
                    if status == "listed":
                        break
                if verdict is None:
                    verdict = {"status": "unchecked", "how": "no name to search", "url": None}
            local[key] = verdict
        with lock:
            results.update(local)
            done[0] += len(pairs)
            if time.time() - last_save[0] > 60:
                last_save[0] = time.time()
                save()
                print(f"[VERIFY] {done[0]}/{todo}", flush=True)

    with ThreadPoolExecutor(_WORKERS) as pool:
        for f in [pool.submit(check_site, d, p) for d, p in sorted(by_domain.items(), key=lambda kv: -len(kv[1]))]:
            f.result()
    save()
    counts: dict[str, int] = {}
    for v in results.values():
        counts[v["status"]] = counts.get(v["status"], 0) + 1
    print(f"[VERIFY] done: {counts}", flush=True)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(Path(sys.argv[1]), Path(sys.argv[2]))
