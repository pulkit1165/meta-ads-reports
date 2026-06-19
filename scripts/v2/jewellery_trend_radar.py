#!/usr/bin/env python3
"""
Global Jewellery Trend Radar — FREE v1 (Shopify best-sellers + recency/movement).

Goal: spot the next best-selling jewellery products in mature markets (USA, EU,
UK, AU) BEFORE they reach India, so Studd Muffyn can launch 3-6 months ahead.

How it works (no paid APIs, no scraping libs — just Shopify's public storefront
JSON):
  • Each tracked brand is a Shopify store that exposes a best-sellers collection
    at /collections/<handle>/products.json. The ORDER of products in that
    collection IS the merchandised best-seller ranking.
  • We pull the current ranking + each product's category (product_type), price,
    and publish date — all free.
  • We persist yesterday's ranking in a hidden "_state" tab, so each run can tell
    which products are NEW to the best-seller list or RISING fast, and which were
    only just published yet already rank high. Those are the early signals.

Outputs (one dated tab per day, like the other reports):
  • KPI strip (brands scanned / products tracked / new today / rising today)
  • TOP MOVERS  — the radar: new + fast-rising + freshly-launched-yet-ranking
  • TREND WATCH — hits against the operator's design watchlist (evil eye, tennis
    bracelet, huggies, signet ring, …)
  • CATEGORY PULSE — which jewellery category is heating up (necklaces/bracelets/…)
  • BEST SELLERS — current top of each brand's list (reference)

Paid signals (SimilarWeb traffic, Charm revenue, social mentions) are NOT in v1.
They can be layered later to CONFIRM products this radar already flags.

Run by .github/workflows/jewellery-trend-radar.yml on a daily cron. Needs only
GOOGLE_SERVICE_ACCOUNT_FILE (the antriksh-bot@ service account, Editor on the
sheet). No Meta token required.

Usage:
  python3 scripts/v2/jewellery_trend_radar.py            # build + push
  python3 scripts/v2/jewellery_trend_radar.py --create   # create the sheet once
"""
import sys
import time
from datetime import datetime, timezone
from collections import defaultdict

import requests
import gspread
from google.oauth2.service_account import Credentials

import os

# Operator's "Jewellery Trend Radar" sheet. Created once via --create (the SA
# owns it and shares it to the operator). Override with env if ever re-created.
SHEET_ID = os.environ.get("JEWELLERY_RADAR_SHEET_ID", "12ar4xGIsd4i1A6HZ7pr7_CG4ripp5JFypH4wUfBymnk")
SHARE_WITH = "pulkitsharma1165@gmail.com"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TOP_PER_BRAND = 60          # how deep into each best-seller list we rank
BEST_SELLERS_SHOWN = 12     # rows shown per brand in the reference table
TOP_MOVERS_SHOWN = 30

# Approx FX → INR (static; refresh occasionally). Labeled "approx" in the sheet
# so nobody treats it as a live rate. Only used to give an Indian price feel.
FX_INR = {"USD": 86, "EUR": 93, "GBP": 109, "AUD": 56, "INR": 1}

# Tracked Shopify brands. `base` includes any locale path. `bestseller` is the
# collection handle whose order = best-seller ranking.
BRANDS = [
    {"name": "Gorjana",        "country": "USA",       "base": "https://www.gorjana.com",        "currency": "USD", "bestseller": "best-sellers"},
    {"name": "PDPAOLA",        "country": "Spain",     "base": "https://www.pdpaola.com",        "currency": "EUR", "bestseller": "best-sellers"},
    {"name": "Purelei",        "country": "Germany",   "base": "https://purelei.com",            "currency": "EUR", "bestseller": "bestseller"},
    {"name": "Astrid & Miyu",  "country": "UK",        "base": "https://www.astridandmiyu.com",  "currency": "GBP", "bestseller": "best-sellers"},
    {"name": "Missoma",        "country": "UK",        "base": "https://www.missoma.com",        "currency": "GBP", "bestseller": "best-sellers"},
    {"name": "By Charlotte",   "country": "Australia", "base": "https://www.bycharlotte.com.au", "currency": "AUD", "bestseller": "best-sellers"},
    # ── India (direct competitors — affordable demi-fine / fashion jewellery) ──
    {"name": "GIVA",           "country": "India",     "base": "https://giva.co",                "currency": "INR", "bestseller": "bestsellers"},
    {"name": "Palmonas",       "country": "India",     "base": "https://www.palmonas.com",       "currency": "INR", "bestseller": "best-seller"},
    {"name": "Salty",          "country": "India",     "base": "https://salty.co.in",            "currency": "INR", "bestseller": "best-sellers-for-salty"},
    {"name": "Voylla",         "country": "India",     "base": "https://www.voylla.com",         "currency": "INR", "bestseller": "silver-best-sellers"},
    {"name": "Quirksmith",     "country": "India",     "base": "https://quirksmith.com",         "currency": "INR", "bestseller": "best-selling-products"},
]

# Operator design watchlist (Module 4) → matched against title + tags + type.
TREND_KEYWORDS = {
    "Initial / letter necklace": ["initial", "letter", "alphabet"],
    "Birthstone":                ["birthstone", "birth stone"],
    "Chunky chain":              ["chunky", "bold chain", "thick chain"],
    "Coin pendant":              ["coin"],
    "3-bead bracelet":           ["3 bead", "three bead", "trio bead"],
    "Couple bracelet":           ["couple", "his and hers", "matching"],
    "Evil eye":                  ["evil eye", "nazar"],
    "Tennis bracelet/necklace":  ["tennis"],
    "Stackable ring":            ["stackable", "stacking", "stack"],
    "Signet ring":               ["signet"],
    "Huggie earring":            ["huggie", "huggies"],
    "Pearl":                     ["pearl"],
}


# ── Shopify fetch ─────────────────────────────────────────────────────────────
def fetch_collection(base, handle, limit=250, retries=3):
    """Return products (in collection / best-seller order) or [] on failure."""
    url = f"{base}/collections/{handle}/products.json"
    for attempt in range(retries):
        try:
            r = requests.get(url, params={"limit": limit},
                             headers={"User-Agent": UA, "Accept": "application/json"},
                             timeout=20)
            if r.status_code == 200:
                return (r.json() or {}).get("products") or []
            print(f"    {handle}: HTTP {r.status_code}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"    {handle}: {e} (try {attempt+1})", file=sys.stderr)
        time.sleep(1.5 * (attempt + 1))
    return []


def min_price(product):
    prices = []
    for v in product.get("variants") or []:
        try:
            prices.append(float(v.get("price")))
        except (TypeError, ValueError):
            pass
    return min(prices) if prices else 0.0


def normalize_category(product):
    hay = " ".join([
        (product.get("product_type") or ""),
        (product.get("title") or ""),
        " ".join(product.get("tags") or []) if isinstance(product.get("tags"), list)
        else (product.get("tags") or ""),
    ]).lower()
    if any(k in hay for k in ("necklace", "pendant", "chain", "choker")):
        return "Necklaces"
    if any(k in hay for k in ("bracelet", "bangle", "cuff")):
        return "Bracelets"
    if any(k in hay for k in ("earring", "huggie", "stud", "hoop")):
        return "Earrings"
    if "ring" in hay:
        return "Rings"
    if "anklet" in hay:
        return "Anklets"
    if "charm" in hay:
        return "Charms"
    return "Other"


def days_since_published(product):
    raw = product.get("published_at") or product.get("created_at")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).days
    except ValueError:
        return None


def collect():
    """Return list of product dicts with rank/category/price/recency per brand."""
    rows = []
    for b in BRANDS:
        prods = fetch_collection(b["base"], b["bestseller"])
        print(f"  {b['name']} ({b['country']}): {len(prods)} best-seller products")
        rank = 0
        for p in prods:
            handle = (p.get("handle") or "")
            if "gift-card" in handle or "gift card" in (p.get("title") or "").lower():
                continue  # not a real product
            rank += 1
            if rank > TOP_PER_BRAND:
                break
            price = min_price(p)
            cur = b["currency"]
            rows.append({
                "brand": b["name"], "country": b["country"], "currency": cur,
                "handle": p.get("handle") or "",
                "title": (p.get("title") or "").strip(),
                "rank": rank,
                "category": normalize_category(p),
                "price": price,
                "inr": round(price * FX_INR.get(cur, 0)),
                "days": days_since_published(p),
                "url": f"{b['base']}/products/{p.get('handle','')}",
                "hay": " ".join([
                    (p.get("title") or ""),
                    (p.get("product_type") or ""),
                    " ".join(p.get("tags") or []) if isinstance(p.get("tags"), list) else (p.get("tags") or ""),
                ]).lower(),
            })
        time.sleep(0.4)
    return rows


# ── state (hidden tab) for day-over-day rank movement ──────────────────────────
def open_sheet():
    sa = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
    if not sa or not os.path.isfile(sa):
        sys.exit(f"GOOGLE_SERVICE_ACCOUNT_FILE missing or invalid: {sa}")
    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(sa, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SHEET_ID)


def read_state(sh):
    """key 'brand||handle' -> {rank, first_seen, price}. Empty on first run."""
    try:
        ws = sh.worksheet("_state")
    except gspread.WorksheetNotFound:
        return {}
    out = {}
    for r in ws.get_all_values()[1:]:  # skip header
        if len(r) < 5 or not r[0]:
            continue
        try:
            out[f"{r[0]}||{r[1]}"] = {"rank": int(r[2]), "first_seen": r[3],
                                      "price": float(r[4] or 0)}
        except ValueError:
            continue
    return out


def write_state(sh, rows, today):
    try:
        ws = sh.worksheet("_state")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="_state", rows=2000, cols=5)
    data = [["brand", "handle", "rank", "first_seen", "price"]]
    for r in rows:
        data.append([r["brand"], r["handle"], r["rank"], r["first_seen"], r["price"]])
    ws.clear()
    ws.update(range_name="A1", values=data, value_input_option="RAW")


def score_movement(rows, state, today):
    """Annotate each row with first_seen / is_new / rank_delta / momentum."""
    first_run = len(state) == 0
    for r in rows:
        key = f"{r['brand']}||{r['handle']}"
        prev = state.get(key)
        if prev is None:
            r["first_seen"] = today
            r["is_new"] = (not first_run)
            r["rank_delta"] = None
        else:
            r["first_seen"] = prev["first_seen"] or today
            r["is_new"] = False
            r["rank_delta"] = prev["rank"] - r["rank"]  # +ve = climbed up

        d = r["days"]
        s = 0.0
        if d is not None:
            if d <= 30:   s += 40
            elif d <= 60: s += 28
            elif d <= 90: s += 18
            elif d <= 180: s += 8
        if r["is_new"]:
            s += 45
        elif r["rank_delta"]:
            if r["rank_delta"] > 0:
                s += min(40, r["rank_delta"] * 2.5)
            else:
                s += max(-20, r["rank_delta"] * 1.5)
        s += max(0.0, 20 - r["rank"] * 0.4)  # being near the top counts a little
        r["momentum"] = round(max(0.0, min(100.0, s)))
    return first_run


# ── sheet writing ─────────────────────────────────────────────────────────────
def _link(url, text):
    return '=HYPERLINK("%s","%s")' % (url, (text or "").replace('"', '""')[:70])


def _delta_str(r, first_run):
    if first_run:
        return "—"
    if r["is_new"]:
        return "🆕 NEW"
    if r["rank_delta"] is None:
        return "—"
    if r["rank_delta"] > 0:
        return f"▲ {r['rank_delta']}"
    if r["rank_delta"] < 0:
        return f"▼ {abs(r['rank_delta'])}"
    return "="


def write_sheet(rows, first_run, today):
    sh = open_sheet()
    titles = {w.title: w for w in sh.worksheets()}
    if today in titles:
        ws = titles[today]
    elif "Sheet1" in titles:
        ws = titles["Sheet1"]; ws.update_title(today)
    else:
        ws = sh.add_worksheet(title=today, rows=400, cols=11)

    n_brands = len({r["brand"] for r in rows})
    n_new = sum(1 for r in rows if r["is_new"])
    n_rising = sum(1 for r in rows if (r["rank_delta"] or 0) > 0)
    cat_counts = defaultdict(int)
    for r in rows:
        cat_counts[r["category"]] += 1
    hottest = max(cat_counts.items(), key=lambda kv: kv[1])[0] if cat_counts else "—"
    stamp = datetime.now(timezone.utc).astimezone().strftime("%d %b %y, %H:%M")

    V = []  # values; we track 1-based section rows for formatting
    V.append(["💎 Global Jewellery Trend Radar"])
    V.append([f"Free v1 — Shopify best-sellers across {n_brands} brands (USA/EU/UK/AU). "
              f"Spots NEW + fast-RISING + freshly-launched products before India. "
              f"Built {stamp}." + (" FIRST RUN — movement starts tomorrow." if first_run else "")])
    V.append([])
    V.append(["Brands scanned", "Products tracked", "🆕 New today", "▲ Rising today", "🔥 Hottest category"])
    V.append([n_brands, len(rows), n_new, n_rising, hottest])
    kpi_val = len(V)
    V.append([])

    # ── TOP MOVERS ──
    movers = sorted(rows, key=lambda r: r["momentum"], reverse=True)[:TOP_MOVERS_SHOWN]
    tm_title = len(V) + 1
    V.append([f"🚀 TOP MOVERS — new / fast-rising / freshly-launched-yet-ranking (Momentum 0-100)"])
    tm_hdr = len(V) + 1
    V.append(["#", "Product", "Brand", "Country", "Category", "Price", "≈₹ (approx)",
              "Rank", "Δ vs prev", "Days live", "Momentum"])
    tm_first = len(V) + 1
    for i, r in enumerate(movers, 1):
        V.append([i, _link(r["url"], r["title"]), r["brand"], r["country"], r["category"],
                  f"{r['currency']} {r['price']:.0f}", r["inr"], r["rank"],
                  _delta_str(r, first_run),
                  ("?" if r["days"] is None else r["days"]), r["momentum"]])
    tm_last = len(V)
    V.append([])

    # ── TREND WATCH (operator design watchlist) ──
    tw_title = len(V) + 1
    V.append(["🔎 TREND WATCH — your design watchlist across all tracked brands"])
    tw_hdr = len(V) + 1
    V.append(["Trend", "# products", "🆕 new/rising", "Brands carrying it", "Cheapest ≈₹"])
    tw_first = len(V) + 1
    for label, kws in TREND_KEYWORDS.items():
        hits = [r for r in rows if any(k in r["hay"] for k in kws)]
        if not hits:
            V.append([label, 0, 0, "—", ""]); continue
        nr = sum(1 for r in hits if r["is_new"] or (r["rank_delta"] or 0) > 0)
        brands = ", ".join(sorted({r["brand"] for r in hits}))
        cheapest = min((r["inr"] for r in hits if r["inr"]), default="")
        V.append([label, len(hits), nr, brands, cheapest])
    tw_last = len(V)
    V.append([])

    # ── CATEGORY PULSE ──
    cp_title = len(V) + 1
    V.append(["📊 CATEGORY PULSE — where the volume + freshness is"])
    cp_hdr = len(V) + 1
    V.append(["Category", "# products", "🆕 new", "▲ rising", "Avg price ≈₹"])
    cp_first = len(V) + 1
    cat_rows = sorted(cat_counts.items(), key=lambda kv: kv[1], reverse=True)
    for cat, cnt in cat_rows:
        sub = [r for r in rows if r["category"] == cat]
        nnew = sum(1 for r in sub if r["is_new"])
        nris = sum(1 for r in sub if (r["rank_delta"] or 0) > 0)
        inrs = [r["inr"] for r in sub if r["inr"]]
        avg = round(sum(inrs) / len(inrs)) if inrs else ""
        V.append([cat, cnt, nnew, nris, avg])
    cp_last = len(V)
    V.append([])

    # ── BEST SELLERS (reference, top N per brand) ──
    bs_title = len(V) + 1
    V.append([f"🏆 BEST SELLERS — current top {BEST_SELLERS_SHOWN} per brand (reference)"])
    bs_hdr = len(V) + 1
    V.append(["Brand", "Rank", "Product", "Category", "Price", "≈₹", "Days live", "Δ vs prev"])
    bs_first = len(V) + 1
    by_brand = defaultdict(list)
    for r in rows:
        by_brand[r["brand"]].append(r)
    for b in BRANDS:
        for r in sorted(by_brand.get(b["name"], []), key=lambda r: r["rank"])[:BEST_SELLERS_SHOWN]:
            V.append([r["brand"], r["rank"], _link(r["url"], r["title"]), r["category"],
                      f"{r['currency']} {r['price']:.0f}", r["inr"],
                      ("?" if r["days"] is None else r["days"]), _delta_str(r, first_run)])
    bs_last = len(V)

    ws.clear()
    ws.update(range_name="A1", values=V, value_input_option="USER_ENTERED")

    # ── formatting ──
    sid = ws.id
    grey = {"red": 0.91, "green": 0.91, "blue": 0.91}
    lgrey = {"red": 0.96, "green": 0.96, "blue": 0.96}
    white = {"red": 1, "green": 1, "blue": 1}
    black = {"red": 0, "green": 0, "blue": 0}

    def fmt(r0, r1, c0, c1, body, fields):
        return {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": r0, "endRowIndex": r1,
                      "startColumnIndex": c0, "endColumnIndex": c1},
            "cell": {"userEnteredFormat": body}, "fields": fields}}

    def heading(title_row, hdr_row, ncols):
        return [
            fmt(title_row - 1, title_row, 0, ncols,
                {"backgroundColor": grey, "textFormat": {"bold": True, "fontSize": 11}},
                "userEnteredFormat(backgroundColor,textFormat)"),
            fmt(hdr_row - 1, hdr_row, 0, ncols,
                {"backgroundColor": lgrey, "textFormat": {"bold": True}},
                "userEnteredFormat(backgroundColor,textFormat)"),
        ]

    reqs = [
        fmt(0, 1, 0, 11,
            {"backgroundColor": {"red": 0.12, "green": 0.12, "blue": 0.2},
             "textFormat": {"bold": True, "fontSize": 14, "foregroundColor": white}},
            "userEnteredFormat(backgroundColor,textFormat)"),
        fmt(3, 4, 0, 5, {"backgroundColor": lgrey, "textFormat": {"bold": True},
                         "horizontalAlignment": "CENTER"},
            "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)"),
        fmt(4, 5, 0, 5, {"textFormat": {"bold": True, "fontSize": 12},
                         "horizontalAlignment": "CENTER"},
            "userEnteredFormat(textFormat,horizontalAlignment)"),
    ]
    reqs += heading(tm_title, tm_hdr, 11)
    reqs += heading(tw_title, tw_hdr, 5)
    reqs += heading(cp_title, cp_hdr, 5)
    reqs += heading(bs_title, bs_hdr, 8)

    # Shade the top 5 movers green so the eye lands there first.
    lgreen = {"red": 0.86, "green": 0.94, "blue": 0.86}
    for i in range(min(5, len(movers))):
        rr = tm_first + i
        reqs.append(fmt(rr - 1, rr, 0, 11, {"backgroundColor": lgreen},
                        "userEnteredFormat(backgroundColor)"))

    reqs += [
        {"updateSheetProperties": {
            "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": kpi_val}},
            "fields": "gridProperties.frozenRowCount"}},
        {"autoResizeDimensions": {
            "dimensions": {"sheetId": sid, "dimension": "COLUMNS",
                           "startIndex": 0, "endIndex": 11}}},
    ]
    sh.batch_update({"requests": reqs})
    return today, len(rows), n_new, n_rising, hottest


# ── one-time sheet creation ────────────────────────────────────────────────────
def create_sheet():
    sa = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(sa, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.create("Jewellery Trend Radar (Global)")
    sh.share(SHARE_WITH, perm_type="user", role="writer")
    print(f"Created sheet: {sh.id}")
    print(f"URL: https://docs.google.com/spreadsheets/d/{sh.id}")
    print(f"Shared with {SHARE_WITH}. Put this id in SHEET_ID / the workflow env.")
    return sh.id


def dry_run():
    """Scrape + score with no prior state and print to stdout (no sheet needed)."""
    rows = collect()
    if not rows:
        sys.exit("No products fetched.")
    today = datetime.now(timezone.utc).astimezone().date().isoformat()
    score_movement(rows, {}, today)
    movers = sorted(rows, key=lambda r: r["momentum"], reverse=True)[:20]
    print("\n=== TOP MOVERS (first-run momentum = recency + rank) ===")
    print(f"{'#':>2} {'Momentum':>8} {'Rank':>4} {'Days':>4}  {'Brand':<14} {'Cat':<10} {'≈₹':>7}  Product")
    for i, r in enumerate(movers, 1):
        d = "?" if r["days"] is None else r["days"]
        print(f"{i:>2} {r['momentum']:>8} {r['rank']:>4} {str(d):>4}  {r['brand']:<14} "
              f"{r['category']:<10} {r['inr']:>7}  {r['title'][:48]}")
    cats = defaultdict(int)
    for r in rows:
        cats[r["category"]] += 1
    print("\n=== CATEGORY PULSE ===")
    for c, n in sorted(cats.items(), key=lambda kv: -kv[1]):
        print(f"  {c:<12} {n}")
    print(f"\nTotal products: {len(rows)} across {len({r['brand'] for r in rows})} brands")


def main():
    if "--create" in sys.argv:
        create_sheet(); return
    if "--dry-run" in sys.argv:
        dry_run(); return
    print(f"Jewellery Trend Radar — {datetime.now(timezone.utc).astimezone():%d %b %y %H:%M}")
    rows = collect()
    if not rows:
        sys.exit("No products fetched from any brand — not writing sheet.")
    today = datetime.now(timezone.utc).astimezone().date().isoformat()
    sh = open_sheet()
    state = read_state(sh)
    first_run = score_movement(rows, state, today)
    title, n, n_new, n_rising, hottest = write_sheet(rows, first_run, today)
    write_state(sh, rows, today)
    print()
    print(f"  {n} products · {n_new} new · {n_rising} rising · hottest: {hottest}")
    print(f"  Written to tab '{title}' in sheet {SHEET_ID}"
          + ("  (first run — movement begins next run)" if first_run else ""))


if __name__ == "__main__":
    main()
