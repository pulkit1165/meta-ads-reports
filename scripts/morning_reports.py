"""
Daily 8 AM IST morning reports — unified orchestrator.

Reports built (target date = yesterday IST):
  1. Yesterday Orders Breakdown   (segment / category / old-new / movers)
  2. Aged Camps >7d               (Sales vs Retarget · day + 7d ROAS)
  3. Product × Portal Structure   (segregated by coverage block)
  4. Product Camp Structure       (per-product full camp list)
  5. Sales Success Rate           (day + 7d win rate by segment)

For each report we maintain:
  - "Latest" tab (overwritten daily)
  - "DD-MMM <ReportName>" archive tab (one snapshot per run)
  - Archives older than ARCHIVE_RETAIN_DAYS are deleted automatically

A "📅 Daily KPIs" tab gets one summary row appended per run for trend tracking.
"""
import os, json, re, sys, time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
import gspread
from google.oauth2.service_account import Credentials

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

# ─── Config ───────────────────────────────────────────────────────────────────
GRAPH = "https://graph.facebook.com/v19.0"


def _load_token():
    tok = os.environ.get("META_ACCESS_TOKEN")
    if tok: return tok
    envf = REPO / ".env"
    if envf.exists():
        for line in envf.read_text().splitlines():
            if line.startswith("META_ACCESS_TOKEN="):
                return line.split("=", 1)[1].strip()
    return None


TOKEN = _load_token()
if not TOKEN:
    print("ERROR: META_ACCESS_TOKEN not set in env or .env file")
    sys.exit(2)

SHEET_ID = os.environ.get(
    "EVERYDAY_REPORT_SHEET_ID",
    "1GZQ3VwjjzmnGmVoxS8KZwuQhHk55RMT7g6Wq184EHSA",
)
SA_FILE = os.environ.get(
    "GOOGLE_SERVICE_ACCOUNT_FILE",
    str(REPO / "google-service-account.json"),
)

ARCHIVE_RETAIN_DAYS = 14
TZ_IST = ZoneInfo("Asia/Kolkata")

# Run date = yesterday IST (data is for the just-finished calendar day)
NOW_IST = datetime.now(TZ_IST)
TODAY = NOW_IST.date() - timedelta(days=1)
YESTERDAY = TODAY - timedelta(days=1)
DAY_BEFORE = YESTERDAY  # alias used in headers
SINCE_7D = TODAY - timedelta(days=6)
DATE_TAG = TODAY.strftime("%d-%b")          # "10-May"
DATE_LONG = TODAY.strftime("%d %b %Y")      # "10 May 2026"
LAUNCH_CUTOFF = TODAY - timedelta(days=8)   # "NEW" = aged ≤8 days

print(f"[morning_reports] Target date (IST): {TODAY}  ·  archive tag: {DATE_TAG!r}")

# Ad accounts — from config/accounts.env (loaded as env vars)
PORTALS = {
    "SM": [
        ("SM_FRAGRANCE_01",   os.environ.get("SM_FRAGRANCE_01",   "act_466922745634023")),
        ("SM_SKIN",           os.environ.get("SM_SKIN",           "act_578075381064759")),
        ("SM_HAIR",           os.environ.get("SM_HAIR",           "act_944634709928295")),
        ("SM_CRYSTALS",       os.environ.get("SM_CRYSTALS",       "act_1181596092752041")),
        ("SM_PERFUME",        os.environ.get("SM_PERFUME",        "act_935485377999005")),
        ("SM_CREDIT_LINE_05", os.environ.get("SM_CREDIT_LINE_05", "act_1196805438052488")),
    ],
    "SML": [
        ("SML_SKIN",     os.environ.get("SML_SKIN",     "act_918587349998103")),
        ("SML_HAIR",     os.environ.get("SML_HAIR",     "act_1229831035065328")),
        ("SML_CRYSTALS", os.environ.get("SML_CRYSTALS", "act_578721778180301")),
        ("SML_CL_06",    os.environ.get("SML_CL_06",    "act_2227792690890157")),
        ("SML_CL_07",    os.environ.get("SML_CL_07",    "act_830305079053577")),
    ],
    "NBP": [
        ("NBP_SKIN",         os.environ.get("NBP_SKIN",         "act_1505319823511657")),
        ("NBP_HAIR_PERFUME", os.environ.get("NBP_HAIR_PERFUME", "act_1501832634098072")),
        ("NBP_CRYSTALS",     os.environ.get("NBP_CRYSTALS",     "act_1106988370948991")),
    ],
}


# ─── Meta API helpers ─────────────────────────────────────────────────────────
def gget(url, params, retries=6):
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=120)
        except requests.exceptions.RequestException as e:
            time.sleep(15 * (i + 1)); continue
        if r.status_code == 200: return r.json()
        try: err = r.json().get("error", {})
        except: err = {}
        # Rate limit / transient
        if r.status_code in (429, 500, 502, 503, 504) or err.get("code") in (4, 17, 32, 613):
            wait = 30 * (i + 1)
            print(f"    [rate-lim wait {wait}s code={err.get('code')}]")
            time.sleep(wait); continue
        # Permission error → don't retry
        if err.get("code") == 200:
            return {"error": err}
        return r.json()
    return {"error": {"message": "retries-exhausted"}}


def paginate(url, params):
    out = []; p = dict(params); p["access_token"] = TOKEN
    while url:
        d = gget(url, p)
        if "error" in d: return d
        out.extend(d.get("data", []))
        nxt = d.get("paging", {}).get("next")
        if not nxt: break
        url, p = nxt, {}
    return out


def pick_revenue(av):
    if not av: return 0.0
    for x in av:
        if x.get("action_type") == "omni_purchase":
            try: return float(x.get("7d_click") or x.get("value") or 0)
            except: pass
    for x in av:
        if x.get("action_type") == "purchase":
            try: return float(x.get("7d_click") or x.get("value") or 0)
            except: pass
    return 0.0


def pick_orders(actions):
    if not actions: return 0
    for x in actions:
        if x.get("action_type") == "omni_purchase":
            try: return int(float(x.get("7d_click") or x.get("value") or 0))
            except: pass
    for x in actions:
        if x.get("action_type") == "purchase":
            try: return int(float(x.get("7d_click") or x.get("value") or 0))
            except: pass
    return 0


# ─── Product / segment derivation ─────────────────────────────────────────────
from product_catalogue import derive_product_and_category  # noqa: E402


SEGMENT_FROM_AUDIENCE = [
    # (audience-name keyword,                          segment label)
    (["180dp", "180_day"],                              "180dp"),
    (["30dp", "30_day"],                                "30dp"),
    (["30 imp", "30_imp", "30imp", "imp 30", "30dimp"], "30 imp"),
    (["180 imp", "180_imp", "180imp", "180dimp"],       "180 imp"),
    (["atc", "add_to_cart", "add to cart"],             "ATC"),
    (["visitor"],                                        "visitors"),
    (["lookalike", "lal"],                              "Lookalike"),
]


def label_segment(adset):
    """Derive segment from adset's targeting (custom audience inclusions/exclusions).
    Same heuristic as /tmp/yesterday_orders_breakdown.py."""
    targeting = adset.get("targeting") or {}
    incl = (targeting.get("custom_audiences") or [])
    excl = (targeting.get("excluded_custom_audiences") or [])

    incl_names = " | ".join((a.get("name", "") for a in incl)).lower()
    excl_names = " | ".join((a.get("name", "") for a in excl)).lower()

    def match_label(text):
        for kws, lbl in SEGMENT_FROM_AUDIENCE:
            for kw in kws:
                if kw in text: return lbl
        return None

    incl_lbl = match_label(incl_names)
    excl_lbl = match_label(excl_names)

    if incl_lbl: return f"inc {incl_lbl}"
    if excl_lbl: return f"ex {excl_lbl}"

    # Inclusions present but unrecognised
    if incl: return "inc: other"
    # Exclusions present but unrecognised
    if excl: return "ex: other"

    # Lifetime / broad / loose detection from name
    name = (adset.get("name") or "").lower()
    if "loose" in name: return "Loose"
    if "ds" in name.split("_") or name.startswith("ds_") or "_ds_" in name:
        return "DS"
    return "Broad / Lifetime"


def is_sales(seg):
    s = (seg or "").lower()
    return not (s.startswith("inc") or s.startswith("lookalike"))


# ─── Sheets helpers ───────────────────────────────────────────────────────────
def open_sheet():
    creds = Credentials.from_service_account_file(
        SA_FILE,
        scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds).open_by_key(SHEET_ID)


def fmt_money(v):
    if not v: return "₹0"
    n = int(round(v)); s = str(n)
    if len(s) <= 3: return f"₹{s}"
    last3 = s[-3:]; rest = s[:-3]; parts = []
    while len(rest) > 2:
        parts.insert(0, rest[-2:]); rest = rest[:-2]
    if rest: parts.insert(0, rest)
    return "₹" + ",".join(parts) + "," + last3


def pct(a, b):
    if not b: return None
    return (a - b) / b * 100


def emoji(p):
    if p is None: return ""
    if p >= 20: return " 🚀"
    if p <= -20: return " 📉"
    return ""


def fmt_pct(p):
    if p is None: return "—"
    return f"{p:+.0f}%{emoji(p)}"


# ─── Phase 1: Data pull ───────────────────────────────────────────────────────
def fetch_active_campaigns_and_adsets():
    """Pull active campaigns + adsets (with audience targeting) per account.
    Returns dict (portal, name) -> camp_record.
    """
    print(f"\n[Phase 1] Fetching active campaigns + adsets...")
    camps_out = {}    # (portal, campaign_name) -> {portal, name, category, product, segment, ...}
    cid_to_key = {}   # campaign_id -> (portal, name)

    for portal, accts in PORTALS.items():
        for label, act in accts:
            print(f"  {portal}/{label} ({act})")

            # 1) campaigns (active only)
            camps = paginate(
                f"{GRAPH}/{act}/campaigns",
                {"fields": "id,name,effective_status,start_time",
                 "limit": 500,
                 "filtering": json.dumps([{"field": "effective_status", "operator": "IN",
                                           "value": ["ACTIVE"]}])})
            if isinstance(camps, dict) and "error" in camps:
                print(f"    [skip campaigns] {camps['error'].get('message','?')[:90]}")
                continue

            # 2) Adsets per campaign (for audience-based segment label) — batch by account
            adsets = paginate(
                f"{GRAPH}/{act}/adsets",
                {"fields": "id,name,campaign_id,targeting{custom_audiences,excluded_custom_audiences,age_min,age_max}",
                 "limit": 500,
                 "filtering": json.dumps([{"field": "effective_status", "operator": "IN",
                                           "value": ["ACTIVE"]}])})
            if isinstance(adsets, dict) and "error" in adsets:
                adsets = []
            adsets_by_camp = defaultdict(list)
            for a in adsets:
                adsets_by_camp[a.get("campaign_id")].append(a)

            for c in camps:
                cid = c["id"]; name = c.get("name", "")
                # Pick first adset's segment label (most camps are single-adset; if multi,
                # we use the first as representative)
                ad_list = adsets_by_camp.get(cid, [])
                seg = label_segment(ad_list[0]) if ad_list else "Broad / Lifetime"

                product, category = derive_product_and_category(name)
                start_time = c.get("start_time", "")
                first_day = start_time[:10] if start_time else None
                age_days = (TODAY - date.fromisoformat(first_day)).days if first_day else 0

                key = (portal, name)
                camps_out[key] = {
                    "portal": portal, "ad_account": label,
                    "campaign_id": cid, "name": name,
                    "category": category, "product": product,
                    "segment": seg, "type": "Sales" if is_sales(seg) else "Retarget",
                    "first_day": first_day, "age_days": age_days,
                    "is_new": age_days <= 8,
                    "spend_y": 0.0, "rev_y": 0.0, "orders_y": 0,
                    "spend_p": 0.0, "rev_p": 0.0, "orders_p": 0,
                    "sp_7d":   0.0, "rev_7d": 0.0, "ord_7d":  0,
                }
                cid_to_key[cid] = key
            print(f"    {len(camps)} camps, {len(adsets)} adsets")
            time.sleep(2)

    print(f"  Total: {len(camps_out)} active campaigns")
    return camps_out, cid_to_key


def fetch_insights_for_window(act, since, until):
    """Campaign-level insights for a date window."""
    return paginate(
        f"{GRAPH}/{act}/insights",
        {"level": "campaign",
         "fields": "campaign_id,spend,actions,action_values",
         "time_range": json.dumps({"since": str(since), "until": str(until)}),
         "action_attribution_windows": json.dumps(["7d_click"]),
         "use_account_attribution_setting": "false",
         "limit": 500})


def fill_insights(camps_out, cid_to_key):
    """Pull yesterday + day-before + 7d insights per account, attach to camp records."""
    print(f"\n[Phase 2] Pulling insights ({DAY_BEFORE} day before · {TODAY} target · 7d window {SINCE_7D}-{TODAY})")

    windows = [
        ("y", TODAY,      TODAY),       # target day (a.k.a. "yesterday" relative to run time)
        ("p", TODAY - timedelta(days=1), TODAY - timedelta(days=1)),  # prior day
        ("7d", SINCE_7D,  TODAY),       # 7-day window
    ]

    coverage = {tag: 0 for tag, _, _ in windows}

    for portal, accts in PORTALS.items():
        for label, act in accts:
            for tag, since, until in windows:
                ins = fetch_insights_for_window(act, since, until)
                if isinstance(ins, dict) and "error" in ins:
                    print(f"  [skip ins {tag}] {portal}/{label}: {ins['error'].get('message','?')[:60]}")
                    continue
                for r in ins:
                    cid = r.get("campaign_id")
                    key = cid_to_key.get(cid)
                    if not key: continue
                    sp = float(r.get("spend") or 0)
                    rev = pick_revenue(r.get("action_values"))
                    orders = pick_orders(r.get("actions"))
                    if tag == "y":
                        camps_out[key]["spend_y"] = sp
                        camps_out[key]["rev_y"] = rev
                        camps_out[key]["orders_y"] = orders
                    elif tag == "p":
                        camps_out[key]["spend_p"] = sp
                        camps_out[key]["rev_p"] = rev
                        camps_out[key]["orders_p"] = orders
                    else:
                        camps_out[key]["sp_7d"] = sp
                        camps_out[key]["rev_7d"] = rev
                        camps_out[key]["ord_7d"] = orders
                    coverage[tag] += 1
                time.sleep(3)

    print(f"  Insight coverage: y={coverage['y']}, p={coverage['p']}, 7d={coverage['7d']}")


# ─── Phase 3: Build report payloads ──────────────────────────────────────────
def build_yesterday_breakdown(camps):
    """Report 1: Yesterday Orders Breakdown."""
    rows = []
    banner_idx = []
    header_idx = []
    data_idx = []   # (row_idx, delta_pct)

    def add(r): rows.append(r)

    total = {"camps": 0, "sp_y": 0, "rev_y": 0, "ord_y": 0,
             "sp_p": 0, "rev_p": 0, "ord_p": 0}
    for c in camps.values():
        total["camps"] += 1
        for k in ("spend_y", "rev_y", "orders_y", "spend_p", "rev_p", "orders_p"):
            kk = k.replace("spend", "sp").replace("orders", "ord")
            total[kk] += c[k]

    roas_y = total["rev_y"] / total["sp_y"] if total["sp_y"] else 0
    roas_p = total["rev_p"] / total["sp_p"] if total["sp_p"] else 0

    add([f"━━━ YESTERDAY ({DATE_LONG}) ORDERS BREAKDOWN — vs day before ━━━"])
    banner_idx.append(0)
    add([f"Total: {total['camps']} active camps · ₹{total['sp_y']:,.0f} spend → ₹{total['rev_y']:,.0f} rev "
         f"({total['ord_y']} orders, {roas_y:.2f}x)"])
    add([f"vs day before: ₹{total['sp_p']:,.0f} → ₹{total['rev_p']:,.0f} ({total['ord_p']} orders, {roas_p:.2f}x)  "
         f"·  Δ Rev {fmt_pct(pct(total['rev_y'], total['rev_p']))}  ·  Δ Orders {fmt_pct(pct(total['ord_y'], total['ord_p']))}"])
    add([])

    def block(title, rows_dict, key_label, add_subtotal=True):
        add([f"▶ {title}"])
        banner_idx.append(len(rows) - 1)
        add([key_label, "# Camps", "Spend", "Rev", "Orders", "ROAS",
             "Prev Rev", "Prev Orders", "Prev ROAS", "Δ Rev %", "Δ Orders %"])
        header_idx.append(len(rows) - 1)
        items = sorted(rows_dict.items(), key=lambda kv: -kv[1]["rev_y"])
        for k, g in items:
            r_y = g["rev_y"] / g["sp_y"] if g["sp_y"] else 0
            r_p = g["rev_p"] / g["sp_p"] if g["sp_p"] else 0
            d_rev = pct(g["rev_y"], g["rev_p"])
            d_ord = pct(g["ord_y"], g["ord_p"])
            add([k, g["camps"], fmt_money(g["sp_y"]), fmt_money(g["rev_y"]), g["ord_y"],
                 f"{r_y:.2f}x" if g["sp_y"] else "—",
                 fmt_money(g["rev_p"]), g["ord_p"],
                 f"{r_p:.2f}x" if g["sp_p"] else "—",
                 fmt_pct(d_rev), fmt_pct(d_ord)])
            data_idx.append((len(rows) - 1, d_rev))
        add([])

    # Aggregate
    by_seg = defaultdict(lambda: {"camps": 0, "sp_y": 0, "rev_y": 0, "ord_y": 0, "sp_p": 0, "rev_p": 0, "ord_p": 0})
    by_cat = defaultdict(lambda: {"camps": 0, "sp_y": 0, "rev_y": 0, "ord_y": 0, "sp_p": 0, "rev_p": 0, "ord_p": 0})
    by_age = defaultdict(lambda: {"camps": 0, "sp_y": 0, "rev_y": 0, "ord_y": 0, "sp_p": 0, "rev_p": 0, "ord_p": 0})
    by_cat_age = defaultdict(lambda: {"camps": 0, "sp_y": 0, "rev_y": 0, "ord_y": 0, "sp_p": 0, "rev_p": 0, "ord_p": 0})
    for c in camps.values():
        for grp, key in [(by_seg, c["segment"]), (by_cat, c["category"]),
                         (by_age, "NEW (≤8d)" if c["is_new"] else "OLD (>8d)"),
                         (by_cat_age, (c["category"], "NEW (≤8d)" if c["is_new"] else "OLD (>8d)"))]:
            g = grp[key]
            g["camps"] += 1; g["sp_y"] += c["spend_y"]; g["rev_y"] += c["rev_y"]; g["ord_y"] += c["orders_y"]
            g["sp_p"] += c["spend_p"]; g["rev_p"] += c["rev_p"]; g["ord_p"] += c["orders_p"]

    block("BY SEGMENT", by_seg, "Segment")
    block("BY CATEGORY", by_cat, "Category")
    block("OLD vs NEW (8-day cutoff)", by_age, "Age Bucket")

    # Cat × age (compact)
    add(["▶ CATEGORY × OLD/NEW"])
    banner_idx.append(len(rows) - 1)
    add(["Category", "Age", "# Camps", "Spend", "Rev", "Orders", "ROAS", "Prev Rev", "Δ Rev %"])
    header_idx.append(len(rows) - 1)
    cats_sorted = sorted({c for c, _ in by_cat_age}, key=lambda c: -by_cat[c]["rev_y"])
    for cat in cats_sorted:
        for age in ("NEW (≤8d)", "OLD (>8d)"):
            g = by_cat_age.get((cat, age))
            if not g or g["camps"] == 0: continue
            r_y = g["rev_y"] / g["sp_y"] if g["sp_y"] else 0
            d_rev = pct(g["rev_y"], g["rev_p"])
            add([cat, age, g["camps"], fmt_money(g["sp_y"]), fmt_money(g["rev_y"]),
                 g["ord_y"], f"{r_y:.2f}x" if g["sp_y"] else "—",
                 fmt_money(g["rev_p"]), fmt_pct(d_rev)])
            data_idx.append((len(rows) - 1, d_rev))
    add([])

    # Big movers
    add(["▶ BIG MOVERS — ≥20% revenue change (min ₹2k either day)"])
    banner_idx.append(len(rows) - 1)
    add(["Portal", "Campaign", "Category", "Product", "Segment", "Age",
         "Spend", "Rev", "Orders", "ROAS", "Prev Rev", "Δ Rev %"])
    header_idx.append(len(rows) - 1)
    movers = []
    for c in camps.values():
        if c["rev_y"] < 2000 and c["rev_p"] < 2000: continue
        d = pct(c["rev_y"], c["rev_p"])
        if d is None or abs(d) < 20: continue
        movers.append((c, d))
    movers.sort(key=lambda x: -x[1])
    for c, d in movers:
        r_y = c["rev_y"] / c["spend_y"] if c["spend_y"] else 0
        add([c["portal"], c["name"], c["category"], c["product"], c["segment"],
             "NEW" if c["is_new"] else "OLD",
             fmt_money(c["spend_y"]), fmt_money(c["rev_y"]), c["orders_y"],
             f"{r_y:.2f}x" if c["spend_y"] else "—",
             fmt_money(c["rev_p"]), fmt_pct(d)])
        data_idx.append((len(rows) - 1, d))

    W = 12
    rows = [r + [""] * (W - len(r)) for r in rows]
    return {"rows": rows, "banners": banner_idx, "headers": header_idx,
            "tints_by_delta": data_idx, "totals": total, "width": W}


def build_aged_camps(camps):
    """Report 2: Aged Camps >7d."""
    aged = [c for c in camps.values() if c["age_days"] > 7]
    rollup = {"Sales": _agg(), "Retarget": _agg()}
    by_seg = defaultdict(_agg)
    total = _agg()
    for c in aged:
        b = "Sales" if is_sales(c["segment"]) else "Retarget"
        for g in (rollup[b], by_seg[c["segment"]], total):
            _accum(g, c)

    rows = []; banner_idx = []; header_idx = []; data_idx = []

    def add(r): rows.append(r)

    add([f"━━━ AGED CAMPS (>7 days) — Sales vs Retarget · {DATE_LONG} ━━━"])
    banner_idx.append(0)
    r_y = total["rev_y"] / total["sp_y"] if total["sp_y"] else 0
    r_7 = total["rev_7"] / total["sp_7"] if total["sp_7"] else 0
    add([f"{len(aged)} aged camps  ·  Yesterday ₹{total['sp_y']:,.0f}→₹{total['rev_y']:,.0f} ({r_y:.2f}x)  ·  "
         f"7d (since {SINCE_7D.strftime('%d %b')}) ₹{total['sp_7']:,.0f}→₹{total['rev_7']:,.0f} ({r_7:.2f}x)"])
    add([])

    add(["▶ SALES vs RETARGET ROLLUP"])
    banner_idx.append(len(rows) - 1)
    add(["Bucket", "# Camps", "Yesterday Spend", "Yesterday Rev", "Yesterday Orders", "Yesterday ROAS",
         "7d Spend", "7d Rev", "7d Orders", "7d ROAS"])
    header_idx.append(len(rows) - 1)
    for b in ("Sales", "Retarget"):
        g = rollup[b]
        if g["camps"] == 0: continue
        ry = g["rev_y"] / g["sp_y"] if g["sp_y"] else 0
        r7 = g["rev_7"] / g["sp_7"] if g["sp_7"] else 0
        add([b, g["camps"],
             fmt_money(g["sp_y"]), fmt_money(g["rev_y"]), g["ord_y"],
             f"{ry:.2f}x" if g["sp_y"] else "—",
             fmt_money(g["sp_7"]), fmt_money(g["rev_7"]), g["ord_7"],
             f"{r7:.2f}x" if g["sp_7"] else "—"])
        data_idx.append((len(rows) - 1, r7))
    g = total
    ry = g["rev_y"] / g["sp_y"] if g["sp_y"] else 0
    r7 = g["rev_7"] / g["sp_7"] if g["sp_7"] else 0
    add(["TOTAL", g["camps"], fmt_money(g["sp_y"]), fmt_money(g["rev_y"]), g["ord_y"],
         f"{ry:.2f}x", fmt_money(g["sp_7"]), fmt_money(g["rev_7"]), g["ord_7"],
         f"{r7:.2f}x"])
    total_row_idx = len(rows) - 1
    add([])

    add(["▶ SEGMENT-WISE — Yesterday vs 7d (sorted by 7d spend)"])
    banner_idx.append(len(rows) - 1)
    add(["Segment", "Type", "# Camps",
         "Yesterday Spend", "Yesterday Rev", "Yesterday ROAS",
         "7d Spend", "7d Rev", "7d Orders", "7d ROAS"])
    header_idx.append(len(rows) - 1)
    for seg, g in sorted(by_seg.items(), key=lambda kv: -kv[1]["sp_7"]):
        typ = "Sales" if is_sales(seg) else "Retarget"
        ry = g["rev_y"] / g["sp_y"] if g["sp_y"] else 0
        r7 = g["rev_7"] / g["sp_7"] if g["sp_7"] else 0
        add([seg, typ, g["camps"],
             fmt_money(g["sp_y"]), fmt_money(g["rev_y"]),
             f"{ry:.2f}x" if g["sp_y"] else "—",
             fmt_money(g["sp_7"]), fmt_money(g["rev_7"]), g["ord_7"],
             f"{r7:.2f}x" if g["sp_7"] else "—"])
        data_idx.append((len(rows) - 1, r7))
    add([])

    add([f"▶ CAMPAIGN LIST  ·  {len(aged)} aged camps (sorted by 7d spend)"])
    banner_idx.append(len(rows) - 1)
    add(["#", "Portal", "Type", "Segment", "Age", "Category", "Campaign Name",
         "Yesterday Spend", "Yesterday Rev", "Yesterday ROAS",
         "7d Spend", "7d Rev", "7d Orders", "7d ROAS"])
    header_idx.append(len(rows) - 1)
    for i, c in enumerate(sorted(aged, key=lambda c: -c["sp_7d"]), 1):
        typ = "Sales" if is_sales(c["segment"]) else "Retarget"
        ry = c["rev_y"] / c["spend_y"] if c["spend_y"] else 0
        r7 = c["rev_7d"] / c["sp_7d"] if c["sp_7d"] else 0
        add([i, c["portal"], typ, c["segment"], c["age_days"], c["category"], c["name"],
             fmt_money(c["spend_y"]), fmt_money(c["rev_y"]),
             f"{ry:.2f}x" if c["spend_y"] else "—",
             fmt_money(c["sp_7d"]), fmt_money(c["rev_7d"]), c["ord_7d"],
             f"{r7:.2f}x" if c["sp_7d"] else "—"])
        data_idx.append((len(rows) - 1, r7))

    W = 14
    rows = [r + [""] * (W - len(r)) for r in rows]
    return {"rows": rows, "banners": banner_idx, "headers": header_idx,
            "tints_by_roas": data_idx, "totals": total, "rollup": rollup,
            "subtotal_idx": [total_row_idx], "width": W}


def _agg():
    return {"camps": 0, "sp_y": 0.0, "rev_y": 0.0, "ord_y": 0,
            "sp_7": 0.0, "rev_7": 0.0, "ord_7": 0,
            "sp_p": 0.0, "rev_p": 0.0, "ord_p": 0}


def _accum(g, c):
    g["camps"] += 1
    g["sp_y"] += c["spend_y"]; g["rev_y"] += c["rev_y"]; g["ord_y"] += c["orders_y"]
    g["sp_7"] += c["sp_7d"];   g["rev_7"] += c["rev_7d"]; g["ord_7"] += c["ord_7d"]
    g["sp_p"] += c["spend_p"]; g["rev_p"] += c["rev_p"]; g["ord_p"] += c["orders_p"]


def build_product_portal_structure(camps):
    """Report 3: Product × Portal Structure (segregated by coverage block)."""
    PORTAL_ORDER = ["SM", "SML", "NBP"]
    struct = defaultdict(lambda: defaultdict(lambda: {
        "camps": 0, "sales_segs": set(), "ret_segs": set(),
        "sp_y": 0.0, "rev_y": 0.0}))
    prod_cat = {}
    for c in camps.values():
        g = struct[c["product"]][c["portal"]]
        g["camps"] += 1
        prod_cat[c["product"]] = c["category"]
        if is_sales(c["segment"]): g["sales_segs"].add(c["segment"])
        else: g["ret_segs"].add(c["segment"])
        g["sp_y"] += c["spend_y"]; g["rev_y"] += c["rev_y"]

    def coverage(prod):
        portals = sorted(struct[prod].keys())
        if portals == ["NBP", "SM", "SML"]: return "ALL 3"
        if portals == ["NBP", "SM"]: return "SM + NBP"
        if portals == ["NBP", "SML"]: return "SML + NBP"
        if portals == ["SM", "SML"]: return "SM + SML"
        if portals == ["SM"]: return "SM only"
        if portals == ["NBP"]: return "NBP only"
        if portals == ["SML"]: return "SML only"
        return "+".join(portals)

    blocks = defaultdict(list)
    for prod in struct:
        blocks[coverage(prod)].append(prod)
    for tag in blocks:
        blocks[tag].sort(key=lambda p: -sum(struct[p][q]["rev_y"] for q in struct[p]))

    BLOCK_ORDER = [
        ("ALL 3",      "🟩 ALL 3 PORTALS — flagship products with full coverage",       (0.78, 0.92, 0.78)),
        ("SM + NBP",   "🟨 SM + NBP — missing in SML (SML expansion candidates)",       (0.97, 0.97, 0.78)),
        ("SM + SML",   "🟨 SM + SML — missing in NBP (NBP expansion candidates)",       (0.97, 0.97, 0.78)),
        ("SML + NBP",  "🟨 SML + NBP — missing in SM (verify)",                         (0.97, 0.97, 0.78)),
        ("SM only",    "🟥 SM-ONLY — concentrated in SM, unreplicated",                  (0.99, 0.85, 0.85)),
        ("NBP only",   "🟥 NBP-ONLY — exclusive to NBP",                                  (0.99, 0.85, 0.85)),
        ("SML only",   "🟥 SML-ONLY — exclusive to SML",                                  (0.99, 0.85, 0.85)),
    ]

    def fmt_segs(g):
        if not g: return "—"
        sales = sorted(g["sales_segs"]); ret = sorted(g["ret_segs"])
        parts = []
        if sales: parts.append("S: " + ", ".join(sales))
        if ret: parts.append("R: " + ", ".join(ret))
        return "  ·  ".join(parts) if parts else "—"

    rows = []; banner_idx = []; header_idx = []; tint_rows = []; subtotal_idx = []

    def add(r): rows.append(r)

    portal_camps = defaultdict(int); portal_products = defaultdict(set)
    for prod in struct:
        for portal in struct[prod]:
            portal_camps[portal] += struct[prod][portal]["camps"]
            portal_products[portal].add(prod)

    add([f"━━━ ACTIVE PRODUCT × PORTAL STRUCTURE — segregated by coverage · {DATE_LONG} ━━━"])
    banner_idx.append(0)
    add([f"{len(struct)} active products  ·  {len(camps)} camps  ·  "
         + " | ".join(f"{p}: {len(portal_products[p])} prod / {portal_camps[p]} camps" for p in PORTAL_ORDER)])
    add([])

    add(["▶ COVERAGE SUMMARY"])
    banner_idx.append(len(rows) - 1)
    add(["Block", "# Products", "Yesterday Rev", "Notes"])
    header_idx.append(len(rows) - 1)
    notes_map = {
        "ALL 3":     "Full portal coverage — flagship items",
        "SM + NBP":  "Missing in SML — possible SML gap",
        "SM + SML":  "Missing in NBP — NBP expansion candidate",
        "SML + NBP": "Missing in SM — unusual; verify",
        "SM only":   "Solo in SM — could replicate to NBP/SML",
        "NBP only":  "Solo in NBP",
        "SML only":  "Solo in SML",
    }
    for tag, _, _ in BLOCK_ORDER:
        if tag not in blocks: continue
        rev = sum(sum(struct[p][q]["rev_y"] for q in struct[p]) for p in blocks[tag])
        add([tag, len(blocks[tag]), fmt_money(rev), notes_map.get(tag, "")])
    add([])

    for tag, label, tint in BLOCK_ORDER:
        if tag not in blocks: continue
        prods = blocks[tag]
        block_rev = sum(sum(struct[p][q]["rev_y"] for q in struct[p]) for p in prods)
        add([f"▶ {label}  ·  {len(prods)} products  ·  {fmt_money(block_rev)} rev"])
        banner_idx.append(len(rows) - 1)
        add(["#", "Product", "Category", "Yesterday Rev",
             "SM camps", "SM segments", "SML camps", "SML segments",
             "NBP camps", "NBP segments"])
        header_idx.append(len(rows) - 1)
        for i, prod in enumerate(prods, 1):
            sm = struct[prod].get("SM"); sml = struct[prod].get("SML"); nbp = struct[prod].get("NBP")
            total_rev = sum(struct[prod][p]["rev_y"] for p in struct[prod])
            add([i, prod, prod_cat[prod], fmt_money(total_rev),
                 sm["camps"] if sm else 0, fmt_segs(sm),
                 sml["camps"] if sml else 0, fmt_segs(sml),
                 nbp["camps"] if nbp else 0, fmt_segs(nbp)])
            tint_rows.append((len(rows) - 1, tint))
        n_sm = sum(struct[p]["SM"]["camps"] for p in prods if "SM" in struct[p]) or ""
        n_sml = sum(struct[p]["SML"]["camps"] for p in prods if "SML" in struct[p]) or ""
        n_nbp = sum(struct[p]["NBP"]["camps"] for p in prods if "NBP" in struct[p]) or ""
        add([f"{tag} TOTAL", "", "", fmt_money(block_rev), n_sm, "", n_sml, "", n_nbp, ""])
        subtotal_idx.append(len(rows) - 1)
        add([])

    W = 10
    rows = [r + [""] * (W - len(r)) for r in rows]
    return {"rows": rows, "banners": banner_idx, "headers": header_idx,
            "tint_rows": tint_rows, "subtotal_idx": subtotal_idx,
            "wrap_cols": [5, 7, 9], "width": W}


def build_product_camp_structure(camps):
    """Report 4: Per-product full camp structure."""
    PORTAL_ORDER = ["SM", "SML", "NBP"]
    by_prod = defaultdict(list)
    for c in camps.values():
        by_prod[c["product"]].append(c)
    prod_order = sorted(by_prod.keys(), key=lambda p: -sum(c["rev_y"] for c in by_prod[p]))

    def coverage(prod):
        portals = sorted({c["portal"] for c in by_prod[prod]})
        if portals == ["NBP", "SM", "SML"]: return "🟩 ALL 3"
        if portals == ["NBP", "SM"]: return "🟨 SM+NBP"
        if portals == ["NBP", "SML"]: return "🟨 SML+NBP"
        if portals == ["SM", "SML"]: return "🟨 SM+SML"
        if portals == ["SM"]: return "🟥 SM only"
        if portals == ["NBP"]: return "🟥 NBP only"
        if portals == ["SML"]: return "🟥 SML only"
        return "+".join(portals)

    rows = []; prod_banner_idx = []; portal_subhead_idx = []
    header_idx = []; subtotal_idx = []; data_idx = []

    def add(r): rows.append(r)

    total_rev = sum(c["rev_y"] for c in camps.values())
    total_sp = sum(c["spend_y"] for c in camps.values())
    add([f"━━━ PER-PRODUCT CAMP STRUCTURE — SM × SML × NBP · {DATE_LONG} ━━━"])
    add([f"{len(by_prod)} products · {len(camps)} camps · ₹{total_sp:,.0f} → ₹{total_rev:,.0f} "
         f"({(total_rev/total_sp if total_sp else 0):.2f}x)"])
    add([f"Each block: portal sub-rows (Sales then Retarget by spend desc), product subtotal."])
    add([])

    for pi, prod in enumerate(prod_order, 1):
        plist = by_prod[prod]
        cat = plist[0]["category"]
        cov = coverage(prod)
        p_sp = sum(c["spend_y"] for c in plist); p_rev = sum(c["rev_y"] for c in plist)
        p_ord = sum(c["orders_y"] for c in plist)
        p_roas = (p_rev / p_sp) if p_sp else 0
        add([f"▶ {pi}. {prod}  ·  {cat}  ·  {cov}  ·  {len(plist)} camps  ·  "
             f"₹{p_sp:,.0f}→₹{p_rev:,.0f} ({p_ord} ord, {p_roas:.2f}x)"])
        prod_banner_idx.append(len(rows) - 1)
        add(["#", "Portal", "Type", "Segment", "Age", "Campaign Name",
             "Spend", "Rev", "Orders", "ROAS"])
        header_idx.append(len(rows) - 1)

        by_portal = defaultdict(list)
        for c in plist: by_portal[c["portal"]].append(c)
        seq = 0
        for portal in PORTAL_ORDER:
            pp = by_portal.get(portal, [])
            if not pp: continue
            psp = sum(c["spend_y"] for c in pp); prev = sum(c["rev_y"] for c in pp)
            pord = sum(c["orders_y"] for c in pp)
            proas = (prev / psp) if psp else 0
            add([f"   ━ {portal}", f"{len(pp)} camps", "", "", "", "",
                 fmt_money(psp), fmt_money(prev), pord, f"{proas:.2f}x" if psp else "—"])
            portal_subhead_idx.append(len(rows) - 1)
            for c in sorted(pp, key=lambda c: (0 if is_sales(c["segment"]) else 1, -c["spend_y"])):
                seq += 1
                typ = "Sales" if is_sales(c["segment"]) else "Retarget"
                roas = (c["rev_y"] / c["spend_y"]) if c["spend_y"] else 0
                add([seq, c["portal"], typ, c["segment"], c["age_days"], c["name"],
                     fmt_money(c["spend_y"]), fmt_money(c["rev_y"]), c["orders_y"],
                     f"{roas:.2f}x" if c["spend_y"] else "—"])
                data_idx.append((len(rows) - 1, roas))
        add([f"{prod} TOTAL", "", "", "", "", f"{len(plist)} camps",
             fmt_money(p_sp), fmt_money(p_rev), p_ord,
             f"{p_roas:.2f}x" if p_sp else "—"])
        subtotal_idx.append(len(rows) - 1)
        add([])

    W = 10
    rows = [r + [""] * (W - len(r)) for r in rows]
    return {"rows": rows, "prod_banners": prod_banner_idx,
            "portal_subheads": portal_subhead_idx, "headers": header_idx,
            "subtotal_idx": subtotal_idx, "tints_by_roas": data_idx, "width": W}


def build_sales_success_rate(camps):
    """Report 5: Sales success rate (yesterday + 7d)."""
    sales = [c for c in camps.values() if is_sales(c["segment"])]
    by_seg = defaultdict(lambda: _bucket_init())
    total = _bucket_init()
    for c in sales:
        _bucket_accum(by_seg[c["segment"]], c)
        _bucket_accum(total, c)

    rows = []; banner_idx = []; header_idx = []; data_idx = []

    def add(r): rows.append(r)

    ry = total["rev_y"] / total["sp_y"] if total["sp_y"] else 0
    r7 = total["rev_7"] / total["sp_7"] if total["sp_7"] else 0
    wins_y = total["win_y"] + total["good_y"]
    wins_7 = total["win_7"] + total["good_7"]

    add([f"━━━ SALES BLOCK SUCCESS RATE — Yesterday vs Last 7 Days · {DATE_LONG} ━━━"])
    banner_idx.append(0)
    add([f"{len(sales)} active Sales camps  ·  Yesterday: {total['active_y']} delivered, {ry:.2f}x ROAS  ·  "
         f"7d: {total['active_7']} delivered, {r7:.2f}x ROAS"])
    add(["Success threshold: ROAS ≥ 1.5x ✅. Win % computed against camps that delivered (had spend) in that window."])
    add([])

    add(["▶ ALL SALES — headline success rate"])
    banner_idx.append(len(rows) - 1)
    add(["Window", "Camps", "Delivered", "Spend", "Rev", "ROAS",
         "Wins ≥1.5x", "Win %", "Profitable ≥1x", "Profit %"])
    header_idx.append(len(rows) - 1)
    add(["Yesterday", total["camps"], total["active_y"],
         fmt_money(total["sp_y"]), fmt_money(total["rev_y"]), f"{ry:.2f}x",
         wins_y,
         f"{wins_y/total['active_y']*100:.0f}%" if total["active_y"] else "—",
         wins_y + total["be_y"],
         f"{(wins_y + total['be_y'])/total['active_y']*100:.0f}%" if total["active_y"] else "—"])
    data_idx.append((len(rows) - 1, wins_y / total["active_y"] if total["active_y"] else 0))
    add(["Last 7 Days", total["camps"], total["active_7"],
         fmt_money(total["sp_7"]), fmt_money(total["rev_7"]), f"{r7:.2f}x",
         wins_7,
         f"{wins_7/total['active_7']*100:.0f}%" if total["active_7"] else "—",
         wins_7 + total["be_7"],
         f"{(wins_7 + total['be_7'])/total['active_7']*100:.0f}%" if total["active_7"] else "—"])
    data_idx.append((len(rows) - 1, wins_7 / total["active_7"] if total["active_7"] else 0))
    add([])

    add(["▶ SUCCESS RATE BY SALES SEGMENT — sorted by 7d spend"])
    banner_idx.append(len(rows) - 1)
    add(["Segment", "Total Camps",
         "Y Delivered", "Y Wins ≥1.5x", "Y Win %", "Y ROAS",
         "7d Delivered", "7d Wins ≥1.5x", "7d Win %", "7d ROAS"])
    header_idx.append(len(rows) - 1)
    for seg, g in sorted(by_seg.items(), key=lambda kv: -kv[1]["sp_7"]):
        ry = g["rev_y"] / g["sp_y"] if g["sp_y"] else 0
        r7 = g["rev_7"] / g["sp_7"] if g["sp_7"] else 0
        w_y = g["win_y"] + g["good_y"]; w_7 = g["win_7"] + g["good_7"]
        win_pct_y = w_y / g["active_y"] if g["active_y"] else 0
        win_pct_7 = w_7 / g["active_7"] if g["active_7"] else 0
        add([seg, g["camps"],
             g["active_y"], w_y, f"{win_pct_y*100:.0f}%" if g["active_y"] else "—",
             f"{ry:.2f}x" if g["sp_y"] else "—",
             g["active_7"], w_7, f"{win_pct_7*100:.0f}%" if g["active_7"] else "—",
             f"{r7:.2f}x" if g["sp_7"] else "—"])
        data_idx.append((len(rows) - 1, win_pct_7))
    add([])

    add(["▶ ROAS BUCKET DISTRIBUTION — # camps per band"])
    banner_idx.append(len(rows) - 1)
    add(["Segment", "Camps",
         "Y ⭐≥2x", "Y ✅1.5-2x", "Y ⚠1-1.5x", "Y ❌<1x", "Y ∅No Spend",
         "7d ⭐≥2x", "7d ✅1.5-2x", "7d ⚠1-1.5x"])
    header_idx.append(len(rows) - 1)
    for seg, g in sorted(by_seg.items(), key=lambda kv: -kv[1]["sp_7"]):
        add([seg, g["camps"],
             g["win_y"], g["good_y"], g["be_y"], g["lose_y"], g["ns_y"],
             g["win_7"], g["good_7"], g["be_7"]])
    g = total
    add(["ALL SALES", g["camps"],
         g["win_y"], g["good_y"], g["be_y"], g["lose_y"], g["ns_y"],
         g["win_7"], g["good_7"], g["be_7"]])
    total_bucket_row = len(rows) - 1

    W = 10
    rows = [r + [""] * (W - len(r)) for r in rows]
    return {"rows": rows, "banners": banner_idx, "headers": header_idx,
            "tints_by_winrate": data_idx, "subtotal_idx": [total_bucket_row],
            "totals": total, "width": W}


def _bucket_init():
    return {"camps": 0, "active_y": 0, "active_7": 0,
            "sp_y": 0.0, "rev_y": 0.0, "sp_7": 0.0, "rev_7": 0.0,
            "win_y": 0, "good_y": 0, "be_y": 0, "lose_y": 0, "ns_y": 0,
            "win_7": 0, "good_7": 0, "be_7": 0, "lose_7": 0, "ns_7": 0}


def _bucket_accum(g, c):
    g["camps"] += 1
    g["sp_y"] += c["spend_y"]; g["rev_y"] += c["rev_y"]
    g["sp_7"] += c["sp_7d"]; g["rev_7"] += c["rev_7d"]
    if c["spend_y"] > 0:
        g["active_y"] += 1
        roas = c["rev_y"] / c["spend_y"]
        if roas >= 2.0: g["win_y"] += 1
        elif roas >= 1.5: g["good_y"] += 1
        elif roas >= 1.0: g["be_y"] += 1
        else: g["lose_y"] += 1
    else: g["ns_y"] += 1
    if c["sp_7d"] > 0:
        g["active_7"] += 1
        roas = c["rev_7d"] / c["sp_7d"]
        if roas >= 2.0: g["win_7"] += 1
        elif roas >= 1.5: g["good_7"] += 1
        elif roas >= 1.0: g["be_7"] += 1
        else: g["lose_7"] += 1
    else: g["ns_7"] += 1


# ─── Phase 4: Sheet pusher ────────────────────────────────────────────────────
def _gs_retry(fn, *args, label="", **kwargs):
    """Retry gspread call on 429 with exponential backoff."""
    for i in range(8):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            code = getattr(e, "response", None)
            status = code.status_code if code is not None else 0
            msg = str(e)[:120]
            if status == 429 or "Quota" in msg or "RATE_LIMIT" in msg:
                wait = 30 * (i + 1)
                print(f"    [Sheets 429 {label}] wait {wait}s")
                time.sleep(wait)
                continue
            raise
        except Exception as e:
            if "429" in str(e) or "Quota" in str(e):
                wait = 30 * (i + 1)
                print(f"    [Sheets retry {label}] {str(e)[:80]} wait {wait}s")
                time.sleep(wait); continue
            raise
    raise RuntimeError(f"Sheets retries exhausted for {label}")


def push_payload(sh, tab_name, payload, kind):
    """Push a report payload to a tab (creates or replaces)."""
    rows = payload["rows"]
    W = payload["width"]
    try:
        ws = _gs_retry(sh.worksheet, tab_name, label=f"worksheet({tab_name})")
        _gs_retry(sh.del_worksheet, ws, label=f"del({tab_name})")
    except gspread.exceptions.WorksheetNotFound:
        pass
    ws = _gs_retry(sh.add_worksheet, title=tab_name,
                   rows=max(len(rows) + 20, 200), cols=W,
                   label=f"add({tab_name})")
    last_col = chr(ord("A") + W - 1)
    _gs_retry(ws.update, values=rows, range_name=f"A1:{last_col}{len(rows)}",
              value_input_option="USER_ENTERED",
              label=f"update({tab_name})")

    fmt_reqs = []

    def req(r0, r1, c0, c1, obj, fields):
        return {"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": r0, "endRowIndex": r1,
                      "startColumnIndex": c0, "endColumnIndex": c1},
            "cell": {"userEnteredFormat": obj},
            "fields": fields}}

    # Top banner (always row 0)
    fmt_reqs.append(req(0, 1, 0, W,
        {"backgroundColor": {"red": 0.85, "green": 0.45, "blue": 0.20},
         "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}, "fontSize": 13},
         "horizontalAlignment": "LEFT"},
        "userEnteredFormat.backgroundColor,userEnteredFormat.textFormat,userEnteredFormat.horizontalAlignment"))
    # Sub-info rows in salmon
    fmt_reqs.append(req(1, 4, 0, W,
        {"backgroundColor": {"red": 0.97, "green": 0.92, "blue": 0.85}, "textFormat": {"bold": True}},
        "userEnteredFormat.backgroundColor,userEnteredFormat.textFormat.bold"))

    # Section banners (▶ rows)
    for ri in payload.get("banners", []):
        fmt_reqs.append(req(ri, ri + 1, 0, W,
            {"backgroundColor": {"red": 0.10, "green": 0.20, "blue": 0.45},
             "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}, "fontSize": 11}},
            "userEnteredFormat.backgroundColor,userEnteredFormat.textFormat"))

    # Per-product banners (kind=product_camp)
    for ri in payload.get("prod_banners", []):
        fmt_reqs.append(req(ri, ri + 1, 0, W,
            {"backgroundColor": {"red": 0.10, "green": 0.20, "blue": 0.45},
             "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}, "fontSize": 11}},
            "userEnteredFormat.backgroundColor,userEnteredFormat.textFormat"))

    # Portal sub-headers (kind=product_camp)
    for ri in payload.get("portal_subheads", []):
        fmt_reqs.append(req(ri, ri + 1, 0, W,
            {"backgroundColor": {"red": 0.83, "green": 0.88, "blue": 0.96},
             "textFormat": {"bold": True}},
            "userEnteredFormat.backgroundColor,userEnteredFormat.textFormat.bold"))

    # Header rows
    for ri in payload.get("headers", []):
        fmt_reqs.append(req(ri, ri + 1, 0, W,
            {"backgroundColor": {"red": 0.18, "green": 0.30, "blue": 0.55},
             "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
             "horizontalAlignment": "CENTER"},
            "userEnteredFormat.backgroundColor,userEnteredFormat.textFormat,userEnteredFormat.horizontalAlignment"))

    # Subtotal rows (amber)
    for ri in payload.get("subtotal_idx", []):
        fmt_reqs.append(req(ri, ri + 1, 0, W,
            {"backgroundColor": {"red": 1.0, "green": 0.95, "blue": 0.80},
             "textFormat": {"bold": True}},
            "userEnteredFormat.backgroundColor,userEnteredFormat.textFormat.bold"))

    # Tinting by delta (Yesterday Breakdown)
    for ri, d in payload.get("tints_by_delta", []):
        if d is None: continue
        if d >= 50: bg = {"red": 0.65, "green": 0.88, "blue": 0.65}
        elif d >= 20: bg = {"red": 0.82, "green": 0.94, "blue": 0.82}
        elif d <= -50: bg = {"red": 0.95, "green": 0.65, "blue": 0.65}
        elif d <= -20: bg = {"red": 0.99, "green": 0.85, "blue": 0.85}
        else: continue
        fmt_reqs.append(req(ri, ri + 1, 0, W, {"backgroundColor": bg}, "userEnteredFormat.backgroundColor"))

    # Tinting by ROAS (Aged Camps + Product Camp)
    for ri, roas in payload.get("tints_by_roas", []):
        if roas >= 2.0: bg = {"red": 0.78, "green": 0.92, "blue": 0.78}
        elif roas >= 1.5: bg = {"red": 0.88, "green": 0.96, "blue": 0.88}
        elif roas >= 1.0: bg = {"red": 1.00, "green": 0.97, "blue": 0.85}
        elif roas > 0: bg = {"red": 0.99, "green": 0.88, "blue": 0.88}
        else: continue
        fmt_reqs.append(req(ri, ri + 1, 0, W, {"backgroundColor": bg}, "userEnteredFormat.backgroundColor"))

    # Tinting by win-rate (Sales Success)
    for ri, win in payload.get("tints_by_winrate", []):
        if win >= 0.40: bg = {"red": 0.65, "green": 0.88, "blue": 0.65}
        elif win >= 0.25: bg = {"red": 0.82, "green": 0.94, "blue": 0.82}
        elif win >= 0.10: bg = {"red": 1.00, "green": 0.97, "blue": 0.80}
        elif win > 0: bg = {"red": 0.99, "green": 0.85, "blue": 0.85}
        else: continue
        fmt_reqs.append(req(ri, ri + 1, 0, W, {"backgroundColor": bg}, "userEnteredFormat.backgroundColor"))

    # Tinting by block coverage (Product × Portal)
    for ri, (rb, gb, bb) in payload.get("tint_rows", []):
        is_sub = " TOTAL" in str(payload["rows"][ri][0] or "")
        fmt_reqs.append(req(ri, ri + 1, 0, W,
            {"backgroundColor": {"red": rb, "green": gb, "blue": bb},
             **({"textFormat": {"bold": True}} if is_sub else {})},
            "userEnteredFormat.backgroundColor" + (",userEnteredFormat.textFormat.bold" if is_sub else "")))

    # Wrap columns
    for c in payload.get("wrap_cols", []):
        fmt_reqs.append(req(0, len(rows), c, c + 1,
                            {"wrapStrategy": "WRAP"}, "userEnteredFormat.wrapStrategy"))

    # Freeze top 4
    fmt_reqs.append({"updateSheetProperties": {
        "properties": {"sheetId": ws.id, "gridProperties": {"frozenRowCount": 4}},
        "fields": "gridProperties.frozenRowCount"}})

    # Push in batches with retry
    for i in range(0, len(fmt_reqs), 200):
        _gs_retry(sh.batch_update, {"requests": fmt_reqs[i:i + 200]},
                  label=f"format-batch({tab_name})")
    return ws


# ─── Phase 5: Daily KPIs trend tab ────────────────────────────────────────────
def append_daily_kpis(sh, payloads, camps):
    """One row per run capturing headline KPIs for trend tracking."""
    tab = "📅 Daily KPIs"
    headers = ["Date", "Camps", "Spend", "Rev", "Orders", "ROAS",
               "Sales Camps", "Sales Win %", "Sales 7d ROAS",
               "Retarget Camps", "Retarget 7d ROAS",
               "NEW (≤8d) Rev", "OLD (>8d) Rev",
               "Top Category", "Top Cat Rev"]
    try:
        ws = _gs_retry(sh.worksheet, tab, label=f"worksheet({tab})")
    except gspread.exceptions.WorksheetNotFound:
        ws = _gs_retry(sh.add_worksheet, title=tab, rows=400, cols=len(headers),
                       label=f"add({tab})")
        _gs_retry(ws.update, values=[headers], range_name=f"A1:O1",
                  value_input_option="USER_ENTERED", label=f"hdr({tab})")
        _gs_retry(sh.batch_update, {"requests": [{"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": len(headers)},
            "cell": {"userEnteredFormat": {
                "backgroundColor": {"red": 0.18, "green": 0.30, "blue": 0.55},
                "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}}}},
            "fields": "userEnteredFormat.backgroundColor,userEnteredFormat.textFormat"}}]},
                  label="kpi-header-fmt")

    # Compute KPIs
    sales = [c for c in camps.values() if is_sales(c["segment"])]
    retarget = [c for c in camps.values() if not is_sales(c["segment"])]
    sp_y = sum(c["spend_y"] for c in camps.values()); rev_y = sum(c["rev_y"] for c in camps.values())
    ord_y = sum(c["orders_y"] for c in camps.values())
    roas_y = rev_y / sp_y if sp_y else 0

    sales_active_y = sum(1 for c in sales if c["spend_y"] > 0)
    sales_wins = sum(1 for c in sales if c["spend_y"] > 0 and (c["rev_y"]/c["spend_y"]) >= 1.5)
    sales_win_pct = (sales_wins / sales_active_y * 100) if sales_active_y else 0
    sales_sp_7 = sum(c["sp_7d"] for c in sales); sales_rev_7 = sum(c["rev_7d"] for c in sales)
    sales_roas_7 = sales_rev_7 / sales_sp_7 if sales_sp_7 else 0

    ret_sp_7 = sum(c["sp_7d"] for c in retarget); ret_rev_7 = sum(c["rev_7d"] for c in retarget)
    ret_roas_7 = ret_rev_7 / ret_sp_7 if ret_sp_7 else 0

    new_rev = sum(c["rev_y"] for c in camps.values() if c["is_new"])
    old_rev = sum(c["rev_y"] for c in camps.values() if not c["is_new"])

    by_cat = defaultdict(float)
    for c in camps.values(): by_cat[c["category"]] += c["rev_y"]
    top_cat, top_cat_rev = sorted(by_cat.items(), key=lambda kv: -kv[1])[0] if by_cat else ("—", 0)

    new_row = [
        DATE_LONG, len(camps), fmt_money(sp_y), fmt_money(rev_y), ord_y, f"{roas_y:.2f}x",
        len(sales), f"{sales_win_pct:.0f}%", f"{sales_roas_7:.2f}x",
        len(retarget), f"{ret_roas_7:.2f}x",
        fmt_money(new_rev), fmt_money(old_rev),
        top_cat, fmt_money(top_cat_rev),
    ]
    # Append at first empty row
    existing = _gs_retry(ws.get_all_values, label="kpi-read")
    next_row = max(2, len(existing) + 1)
    _gs_retry(ws.update, values=[new_row], range_name=f"A{next_row}:O{next_row}",
              value_input_option="USER_ENTERED", label="kpi-append")
    print(f"  Daily KPIs row appended at row {next_row}")


# ─── Phase 6: Cleanup old archive tabs ────────────────────────────────────────
ARCHIVE_PREFIX_RE = re.compile(r"^(\d{2})-([A-Z][a-z]{2})\b")
MONTH_NUM = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
             "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}


def cleanup_old_archives(sh):
    """Delete archive tabs older than ARCHIVE_RETAIN_DAYS."""
    cutoff = TODAY - timedelta(days=ARCHIVE_RETAIN_DAYS)
    deleted = 0
    sheets = _gs_retry(sh.worksheets, label="list-sheets")
    for ws in sheets:
        m = ARCHIVE_PREFIX_RE.match(ws.title)
        if not m: continue
        try:
            day = int(m.group(1)); mon = MONTH_NUM[m.group(2)]
            year = TODAY.year if mon <= TODAY.month else TODAY.year - 1
            tab_date = date(year, mon, day)
        except (ValueError, KeyError): continue
        if tab_date < cutoff:
            print(f"  cleanup: deleting {ws.title!r} (date {tab_date})")
            _gs_retry(sh.del_worksheet, ws, label=f"del-old({ws.title})")
            deleted += 1
            time.sleep(2)
    print(f"  Cleanup: deleted {deleted} archive tab(s) older than {ARCHIVE_RETAIN_DAYS} days")


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    camps, cid_to_key = fetch_active_campaigns_and_adsets()
    if not camps:
        print("ERROR: no active campaigns found — aborting"); sys.exit(2)
    fill_insights(camps, cid_to_key)

    # Persist raw data for debugging
    raw_path = REPO / "state" / f"morning_data_{TODAY}.json"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(json.dumps(list(camps.values()), indent=2, default=str))
    print(f"  Raw data saved to {raw_path}")

    # Build payloads
    print("\n[Phase 3] Building report payloads...")
    pl = {
        "yesterday":         build_yesterday_breakdown(camps),
        "aged":              build_aged_camps(camps),
        "product_portal":    build_product_portal_structure(camps),
        "product_camps":     build_product_camp_structure(camps),
        "sales_success":     build_sales_success_rate(camps),
    }
    for k, p in pl.items():
        print(f"  {k}: {len(p['rows'])} rows")

    # Push
    print(f"\n[Phase 4] Pushing to {SHEET_ID}...")
    sh = open_sheet()
    push_plan = [
        ("yesterday",      "Yesterday Breakdown",        f"{DATE_TAG} Yesterday"),
        ("aged",           "Aged Camps >7d",              f"{DATE_TAG} Aged 7d"),
        ("product_portal", "Product × Portal Structure",  f"{DATE_TAG} Prod×Portal"),
        ("product_camps",  "Product Camp Structure",      f"{DATE_TAG} Prod Camps"),
        ("sales_success",  "Sales Success Rate",          f"{DATE_TAG} Sales Success"),
    ]
    for key, latest_name, archive_name in push_plan:
        print(f"  → {latest_name!r}")
        push_payload(sh, latest_name, pl[key], key)
        time.sleep(8)   # pace between pushes to stay under sheets quota
        print(f"  → {archive_name!r} (archive)")
        push_payload(sh, archive_name, pl[key], key)
        time.sleep(8)

    # Daily KPIs trend
    print("\n[Phase 5] Appending to Daily KPIs trend tab...")
    append_daily_kpis(sh, pl, camps)

    # Cleanup
    print("\n[Phase 6] Cleanup old archives...")
    cleanup_old_archives(sh)

    print(f"\n✅ Morning reports complete for {TODAY}")
    print(f"   URL: https://docs.google.com/spreadsheets/d/{SHEET_ID}")


if __name__ == "__main__":
    main()
