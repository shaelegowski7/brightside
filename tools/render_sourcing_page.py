"""Render tools/source_batch.py output as a self-contained HTML page for the
Artifact tool: every pass, its numbers, gating and where to source it.

    python tools/render_sourcing_page.py <sourcing.json> <out.html> [scan date]

Product links are built from the ASIN on the Keepa record and shown beside
Keepa's own title, so the link always opens the product that was scored.
"""
import json
import sys
from pathlib import Path

PAGE = r"""<title>Brightside Sourcing Shortlist</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@500;600;700&family=Barlow:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap">
<style>
/* Layout: a warehouse pick-list. Summary strip, filter bar, then one ruled row per product. */
:root {
  --paper: #f2f4f1; --card: #ffffff; --ink: #1c2629; --muted: #5b6a6e; --line: #d9dfdc;
  --tape: #b8730b; --tape-soft: #f6ead6;
  --good: #2c7a4b; --good-soft: #e1f0e6; --warn: #9a6a00; --warn-soft: #f7eed3;
  --bad: #a3392f; --bad-soft: #f6e1de; --info: #2f5f86; --info-soft: #e0eaf3;
  --display: "Barlow Condensed", "Arial Narrow", sans-serif;
  --body: "Barlow", "Segoe UI", system-ui, sans-serif;
  --mono: "JetBrains Mono", ui-monospace, "Cascadia Mono", Consolas, monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --paper: #141a1c; --card: #1b2326; --ink: #e4ebe8; --muted: #93a3a6; --line: #2c373a;
  --tape: #e0a23e; --tape-soft: #3a2d16; --good: #6cc191; --good-soft: #193325;
  --warn: #e3b64e; --warn-soft: #362c12; --bad: #ec8a7f; --bad-soft: #3a1f1c;
  --info: #84b4dc; --info-soft: #1a2c3b; color-scheme: dark; } }
:root[data-theme="dark"] {
  --paper: #141a1c; --card: #1b2326; --ink: #e4ebe8; --muted: #93a3a6; --line: #2c373a;
  --tape: #e0a23e; --tape-soft: #3a2d16; --good: #6cc191; --good-soft: #193325;
  --warn: #e3b64e; --warn-soft: #362c12; --bad: #ec8a7f; --bad-soft: #3a1f1c;
  --info: #84b4dc; --info-soft: #1a2c3b; color-scheme: dark; }
* { box-sizing: border-box; }
body { background: var(--paper); color: var(--ink); font: 15px/1.5 var(--body); margin: 0; }
.wrap { max-width: 1240px; margin: 0 auto; padding-inline: 16px; padding-block: 28px 64px; display: grid; gap: 20px; }
header { display: grid; gap: 6px; }
.eyebrow { font: 600 12px/1 var(--display); letter-spacing: .14em; text-transform: uppercase; color: var(--tape); }
h1 { font: 700 clamp(30px, 5vw, 42px)/1.05 var(--display); margin: 0; text-wrap: balance; }
.lede { color: var(--muted); margin: 0; max-width: 70ch; }
.strip { display: flex; flex-wrap: wrap; gap: 1px; background: var(--line); border: 1px solid var(--line); }
.stat { background: var(--card); padding: 12px 16px; flex: 1 1 140px; min-width: 0; }
.stat .k { font: 600 11px/1.2 var(--display); letter-spacing: .1em; text-transform: uppercase; color: var(--muted); }
.stat .v { font: 700 28px/1.1 var(--display); font-variant-numeric: tabular-nums; }
.controls { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; position: sticky; top: env(safe-area-inset-top, 0px);
  background: var(--paper); padding-block: 10px; z-index: 2; border-bottom: 1px solid var(--line); }
.controls input, .controls select { font: 15px var(--body); color: var(--ink); background: var(--card); border: 1px solid var(--line); padding: 7px 10px; border-radius: 4px; }
.controls input { flex: 1 1 220px; min-width: 0; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip { font: 600 13px/1 var(--display); letter-spacing: .04em; text-transform: uppercase; padding: 7px 10px; border-radius: 4px;
  border: 1px solid var(--line); background: var(--card); color: var(--muted); cursor: pointer; }
.chip[aria-pressed="true"] { background: var(--ink); color: var(--paper); border-color: var(--ink); }
.chip:focus-visible, a:focus-visible, select:focus-visible, input:focus-visible, button:focus-visible { outline: 2px solid var(--tape); outline-offset: 2px; }
.count { font: 500 13px var(--mono); color: var(--muted); }
.list { display: grid; gap: 0; }
.row { display: grid; grid-template-columns: minmax(0, 2.3fr) minmax(0, 1.25fr) minmax(0, 2.4fr); gap: 18px; padding: 16px 4px; border-bottom: 1px solid var(--line); }
@media (max-width: 860px) { .row { grid-template-columns: minmax(0, 1fr); gap: 10px; } }
.prod { display: grid; gap: 4px; min-width: 0; }
.prod a.t { color: var(--ink); font-weight: 600; text-decoration: none; }
.prod a.t:hover { text-decoration: underline; }
.meta { font: 12.5px var(--mono); color: var(--muted); display: flex; flex-wrap: wrap; gap: 4px 12px; }
.meta a { color: var(--info); }
.nums { display: grid; grid-template-columns: auto auto; gap: 2px 12px; font-variant-numeric: tabular-nums; align-content: start; }
.nums dt { font: 600 11px/1.6 var(--display); letter-spacing: .08em; text-transform: uppercase; color: var(--muted); }
.nums dd { margin: 0; font: 500 14px/1.6 var(--mono); text-align: right; }
.nums dd.buy { color: var(--good); font-weight: 600; }
.tags { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 4px; }
.tag { font: 500 11.5px/1.4 var(--body); padding: 1px 7px; border-radius: 3px; background: var(--paper); border: 1px solid var(--line); color: var(--muted); }
.tag.g-ok { background: var(--good-soft); color: var(--good); border-color: transparent; }
.tag.g-req { background: var(--warn-soft); color: var(--warn); border-color: transparent; }
.tag.g-no { background: var(--bad-soft); color: var(--bad); border-color: transparent; }
.src { display: grid; gap: 6px; min-width: 0; align-content: start; }
.verdict { justify-self: start; font: 600 12px/1 var(--display); letter-spacing: .08em; text-transform: uppercase; padding: 5px 8px; border-radius: 3px; }
.v-stocked, .v-known { background: var(--good-soft); color: var(--good); }
.v-leads { background: var(--info-soft); color: var(--info); }
.v-weak { background: var(--warn-soft); color: var(--warn); }
.v-noroute, .v-none { background: var(--bad-soft); color: var(--bad); }
.v-pending { background: var(--paper); color: var(--muted); border: 1px dashed var(--line); }
.leads { display: grid; gap: 4px; margin: 0; padding: 0; list-style: none; }
.leads li { font-size: 13.5px; line-height: 1.4; min-width: 0; overflow-wrap: anywhere; }
.leads a { color: var(--info); font-weight: 600; }
.leads .kind { font: 500 11px var(--mono); color: var(--muted); }
.leads .st { font-size: 12px; color: var(--tape); }
.ck { font: 600 11px/1.4 var(--body); padding: 1px 6px; border-radius: 3px; margin-left: 6px; white-space: nowrap; }
.ck-listed { background: var(--good-soft); color: var(--good); }
.ck-not_found { background: var(--bad-soft); color: var(--bad); }
.ck-unchecked { background: var(--paper); color: var(--muted); border: 1px solid var(--line); }
a.ck { text-decoration: none; }
.note { font-size: 12.5px; color: var(--bad); }
details summary { cursor: pointer; font-size: 12.5px; color: var(--muted); }
.empty { padding: 32px 4px; color: var(--muted); }
.cats { display: flex; flex-wrap: wrap; gap: 6px; }
.cat { font: 500 13px/1 var(--body); padding: 7px 10px; border-radius: 4px; border: 1px solid var(--line); background: var(--card); color: var(--ink); cursor: pointer; }
.cat b { font: 600 12px var(--mono); color: var(--tape); margin-left: 6px; }
.cat[disabled] { opacity: .45; cursor: default; }
.group { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 14px; padding: 22px 4px 8px; border-bottom: 2px solid var(--ink); scroll-margin-top: 140px; }
.group:first-child { padding-top: 6px; }
.group h2 { font: 700 24px/1.1 var(--display); margin: 0; text-wrap: balance; }
.group .gm { font: 500 12.5px var(--mono); color: var(--muted); }
.group .gm .ok { color: var(--good); }
.group .top { margin-left: auto; font: 600 12px var(--display); letter-spacing: .08em; text-transform: uppercase; color: var(--muted); background: none; border: 0; cursor: pointer; padding: 0; }
footer { font-size: 12.5px; color: var(--muted); max-width: 90ch; }
@media (prefers-reduced-motion: no-preference) { .row { transition: background .15s; } .row:hover { background: var(--card); } }
</style>
<div class="wrap">
  <header>
    <div class="eyebrow">Keepa scan · __DATE__ · 10% ROI · 80p profit</div>
    <h1>Brightside Sourcing Shortlist</h1>
    <p class="lede">Every product that passed the scanner, with the most you can pay, whether Amazon will let you list it, and who could supply it. Grouped by Amazon category, biggest first. Supplier leads come first from your own scans, board and supplier list, then from a web search.</p>
  </header>
  <section class="strip" id="strip" aria-label="Summary"></section>
  <div class="controls">
    <input id="q" type="search" placeholder="Search product, brand, ASIN or supplier" aria-label="Search">
    <div class="chips" id="vchips" role="group" aria-label="Filter by sourcing"></div>
    <select id="gate" aria-label="Filter by gating">
      <option value="">Any gating</option><option value="ungated">Open to sell</option>
      <option value="approval_required">Approval required</option><option value="not_eligible">Not eligible</option>
    </select>
    <select id="chk" aria-label="Filter by supplier check">
      <option value="">Any supplier check</option><option value="listed">Supplier confirmed stocking it</option>
      <option value="none">No supplier confirmed</option>
    </select>
    <select id="cat" aria-label="Filter by category"><option value="">All categories</option></select>
    <select id="sort" aria-label="Sort">
      <option value="sales">Sort: sales a month</option><option value="off">Sort: % off needed (lowest)</option>
      <option value="buy">Sort: max buy price</option><option value="fba">Sort: FBA sellers (fewest)</option>
    </select>
    <span class="count" id="count"></span>
  </div>
  <nav class="cats" id="cats" aria-label="Jump to category"></nav>
  <section class="list" id="list" aria-live="polite"></section>
  <footer>Prices from Keepa (Amazon UK) on the scan date. "Buy at or below" clears 10% ROI and 80p profit after Amazon fees with the digital services fee and VAT, storage, returns, prep and inbound shipping. Product links use the same ASIN as the Keepa record; the title shown is Keepa's. Web leads are search results to verify before contacting anyone. "✓ stocks it" means the supplier's own site search shows the product, by barcode or by name next to the brand; "✗ not in their search" means its public search didn't show it, which isn't proof, since trade sites often hide lines until you log in; "? couldn't check" means the site blocks automated checks, needs a login, or has no search. Hover a badge for the detail.</footer>
</div>
<script type="application/json" id="data">__DATA__</script>
<script>
(function () {
  var rows = JSON.parse(document.getElementById("data").textContent);
  var V = [
    ["stocked", "Stocked", /^STOCKED/], ["known", "Known supplier", /^KNOWN/], ["leads", "Leads online", /^LEADS/],
    ["weak", "Weak leads", /^WEAK/], ["noroute", "Likely no UK route", /^LIKELY/], ["none", "No route found", /^NO ROUTE/],
    ["pending", "Sourcing pending", /^PENDING/]
  ];
  rows.forEach(function (r) { var v = V.find(function (x) { return x[2].test(r.verdict || ""); }) || V[5]; r._v = v[0]; r._vl = v[1];
    r._off = r.buybox_price_pence ? 1 - r.target_buy_price_pence / r.buybox_price_pence : 1;
    r._cat = r.category || "Uncategorised";
    var order = { listed: 0, unchecked: 1, not_found: 2 };
    (r.leads || []).sort(function (x, y) { return (order[(x.check || {}).status] ?? 1) - (order[(y.check || {}).status] ?? 1); });
    r._ok = (r.leads || []).some(function (l) { return l.check && l.check.status === "listed"; });
    r._checked = (r.leads || []).some(function (l) { return l.check; }); });
  var anyChecked = rows.some(function (r) { return r._checked; });
  var catTotals = {}; rows.forEach(function (r) { catTotals[r._cat] = (catTotals[r._cat] || 0) + 1; });
  var catNames = Object.keys(catTotals).sort(function (x, y) { return catTotals[y] - catTotals[x] || x.localeCompare(y); });
  var state = { q: "", v: "", gate: "", cat: "", chk: "", sort: "sales" };
  try { var s = JSON.parse(localStorage.getItem("bss-state") || "{}"); Object.assign(state, s); } catch (e) {}
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function a(href, text, cls) { var e = el("a", cls, text); e.href = href; e.target = "_blank"; e.rel = "noopener"; return e; }
  function gbp(p) { return p == null ? "–" : "£" + (p / 100).toFixed(2); }
  var gateLabel = { ungated: ["Open to sell", "g-ok"], approval_required: ["Approval required", "g-req"], not_eligible: ["Not eligible", "g-no"] };

  var strip = document.getElementById("strip");
  [["Passes", rows.length], ["Supplier known", rows.filter(function (r) { return r._v === "stocked" || r._v === "known"; }).length],
   ["Leads online", rows.filter(function (r) { return r._v === "leads"; }).length],
   ["Stockist confirmed", anyChecked ? rows.filter(function (r) { return r._ok; }).length : "–"],
   ["Open to sell now", rows.filter(function (r) { return r.gating === "ungated"; }).length],
   ["Likely no UK route", rows.filter(function (r) { return r._v === "noroute" || r._v === "none"; }).length]
  ].forEach(function (s) { var d = el("div", "stat"); d.appendChild(el("div", "k", s[0])); d.appendChild(el("div", "v", String(s[1]))); strip.appendChild(d); });

  var vchips = document.getElementById("vchips");
  [["", "All"]].concat(V.map(function (x) { return [x[0], x[1]]; })).forEach(function (c) {
    if (c[0] && !rows.some(function (r) { return r._v === c[0]; })) return;
    var b = el("button", "chip", c[1]); b.type = "button"; b.dataset.v = c[0];
    b.addEventListener("click", function () { state.v = c[0]; render(); }); vchips.appendChild(b); });
  var q = document.getElementById("q"), gate = document.getElementById("gate"), sort = document.getElementById("sort"), cat = document.getElementById("cat");
  catNames.forEach(function (c) { var o = el("option", null, c + " (" + catTotals[c] + ")"); o.value = c; cat.appendChild(o); });
  if (state.cat && !catTotals[state.cat]) state.cat = "";
  q.value = state.q; gate.value = state.gate; sort.value = state.sort; cat.value = state.cat;
  cat.addEventListener("change", function () { state.cat = cat.value; render(); });
  var chk = document.getElementById("chk"); chk.value = state.chk; chk.hidden = !anyChecked;
  chk.addEventListener("change", function () { state.chk = chk.value; render(); });
  q.addEventListener("input", function () { state.q = q.value; render(); });
  gate.addEventListener("change", function () { state.gate = gate.value; render(); });
  sort.addEventListener("change", function () { state.sort = sort.value; render(); });

  function row(r) {
    var d = el("article", "row");
    var p = el("div", "prod");
    p.appendChild(a("https://www.amazon.co.uk/dp/" + encodeURIComponent(r.asin), r.keepa_title || r.title || r.asin, "t"));
    var m = el("div", "meta");
    m.appendChild(el("span", null, r.asin));
    m.appendChild(a("https://keepa.com/#!product/2-" + encodeURIComponent(r.asin), "Keepa"));
    if (r.brand) m.appendChild(el("span", null, r.brand));
    if (r.category) m.appendChild(el("span", null, r.category));
    p.appendChild(m);
    var t = el("div", "tags"); var g = gateLabel[r.gating];
    if (g) { if (r.gating === "approval_required" && r.approval_url) { var ga = a(r.approval_url, g[0] + " · apply", "tag " + g[1]); t.appendChild(ga); } else t.appendChild(el("span", "tag " + g[1], g[0])); }
    else t.appendChild(el("span", "tag", "Gating unchecked"));
    (r.flags || []).forEach(function (f) { var k = f.split(":")[0].replace(/_/g, " "); t.appendChild(el("span", "tag", k)); });
    p.appendChild(t); d.appendChild(p);

    var n = el("dl", "nums");
    [["Sells", gbp(r.buybox_price_pence)], ["Buy at or below", gbp(r.target_buy_price_pence), "buy"],
     ["Off sell price", Math.round(r._off * 100) + "%"], ["Sales a month", r.est_monthly_sales ? String(Math.round(r.est_monthly_sales)) : "–"],
     ["FBA sellers", String(r.fba_offer_count)], ["Rank (90-day)", r.sales_rank_avg90 ? r.sales_rank_avg90.toLocaleString("en-GB") : "–"]
    ].forEach(function (x) { n.appendChild(el("dt", null, x[0])); n.appendChild(el("dd", x[2] || null, x[1])); });
    d.appendChild(n);

    var s = el("div", "src"); s.appendChild(el("span", "verdict v-" + r._v, r._vl));
    if (r.board_note) s.appendChild(el("div", "note", "Board: " + r.board_note));
    if (r.no_route && r.no_route.length) s.appendChild(el("div", "note", r.no_route.join(" · ")));
    var leads = r.leads || [];
    function addLeads(list, into) {
      list.forEach(function (l) { var li = el("li");
        if (l.url) li.appendChild(a(l.url, l.name)); else li.appendChild(el("strong", null, l.name));
        if (l.check) {
          var lab = { listed: "✓ stocks it", not_found: "✗ not in their search", unchecked: "? couldn't check" }[l.check.status] || l.check.status;
          var ck = l.check.url && l.check.status !== "unchecked" ? a(l.check.url, lab, "ck ck-" + l.check.status) : el("span", "ck ck-" + l.check.status, lab);
          ck.title = l.check.how || ""; li.appendChild(ck); }
        li.appendChild(el("span", "kind", "  " + l.kind.replace(/_/g, " ") + " · " + l.source));
        if (l.buy_price_pence != null) li.appendChild(el("span", "kind", " · trade " + gbp(l.buy_price_pence)));
        if (l.status) { li.appendChild(document.createElement("br")); li.appendChild(el("span", "st", "Your status: " + l.status)); }
        into.appendChild(li); }); }
    if (leads.length) { var ul = el("ul", "leads"); addLeads(leads.slice(0, 4), ul); s.appendChild(ul);
      if (leads.length > 4) { var det = el("details"); det.appendChild(el("summary", null, (leads.length - 4) + " more leads"));
        var ul2 = el("ul", "leads"); addLeads(leads.slice(4), ul2); det.appendChild(ul2); s.appendChild(det); } }
    d.appendChild(s);
    return d;
  }
  function render() {
    try { localStorage.setItem("bss-state", JSON.stringify(state)); } catch (e) {}
    [].forEach.call(vchips.children, function (b) { b.setAttribute("aria-pressed", String(b.dataset.v === state.v)); });
    var term = state.q.trim().toLowerCase();
    var out = rows.filter(function (r) {
      if (state.v && r._v !== state.v) return false;
      if (state.gate && r.gating !== state.gate) return false;
      if (state.cat && r._cat !== state.cat) return false;
      if (anyChecked && state.chk === "listed" && !r._ok) return false;
      if (anyChecked && state.chk === "none" && r._ok) return false;
      if (!term) return true;
      var hay = [r.asin, r.keepa_title, r.brand, r.category].concat((r.leads || []).map(function (l) { return l.name; })).join(" ").toLowerCase();
      return hay.indexOf(term) !== -1; });
    var key = { sales: function (r) { return -(r.est_monthly_sales || 0); }, off: function (r) { return r._off; },
      buy: function (r) { return -r.target_buy_price_pence; }, fba: function (r) { return r.fba_offer_count; } }[state.sort];
    out.sort(function (x, y) { return key(x) - key(y); });
    var groups = {}; out.forEach(function (r) { (groups[r._cat] = groups[r._cat] || []).push(r); });
    var order = catNames.filter(function (c) { return groups[c]; })
      .sort(function (x, y) { return groups[y].length - groups[x].length || x.localeCompare(y); });
    var list = document.getElementById("list"); list.textContent = "";
    var cats = document.getElementById("cats"); cats.textContent = "";
    if (!out.length) list.appendChild(el("div", "empty", "No products match these filters. Clear the search or pick another filter."));
    order.forEach(function (c, i) {
      var g = groups[c], id = "cat-" + i;
      var h = el("div", "group"); h.id = id;
      h.appendChild(el("h2", null, c));
      var gm = el("span", "gm", g.length + (g.length === 1 ? " product" : " products") + " · ");
      var open = g.filter(function (r) { return r.gating === "ungated"; }).length;
      gm.appendChild(el("span", open ? "ok" : null, open + " open to sell"));
      h.appendChild(gm);
      var top = el("button", "top", "Top ↑"); top.type = "button";
      top.addEventListener("click", function () { document.getElementById("cats").scrollIntoView({ block: "start" }); });
      h.appendChild(top);
      list.appendChild(h);
      g.forEach(function (r) { list.appendChild(row(r)); });
      if (order.length > 1) {
        var b = el("button", "cat", c); b.type = "button"; b.appendChild(el("b", null, String(g.length)));
        b.addEventListener("click", function () { document.getElementById(id).scrollIntoView({ block: "start" }); });
        cats.appendChild(b); }
    });
    cats.hidden = order.length < 2;
    document.getElementById("count").textContent = out.length + " of " + rows.length;
  }
  render();
})();
</script>
"""


def main(src: Path, out: Path, scan_date: str) -> None:
    rows = json.loads(src.read_text(encoding="utf-8"))
    data = json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")
    out.write_text(PAGE.replace("__DATA__", data).replace("__DATE__", scan_date), encoding="utf-8")
    print(f"wrote {out} ({len(rows)} products)")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    main(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else "")
