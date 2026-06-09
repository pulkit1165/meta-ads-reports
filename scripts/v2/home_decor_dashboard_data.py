#!/usr/bin/env python3
"""
Home-Decor dashboard data builder / API logic.

Produces the JSON the antariksh "Home Decor" module consumes:
  - overview: current running budget (effective Rs/day) + spend / ROAS / orders /
    revenue for a date preset, across all home-decor campaigns
  - campaigns: each home-decor campaign with id, name, budget, #adsets, and
    range metrics (spend/roas/orders)
  - per campaign: its ad sets (id, name, roas, purchases, spend) + ads/creatives
    (id, name, thumbnail) so the category lead can view creatives

Home-decor scope mirrors home_decor_sheet.py: derive_category_v2 == 'Crystal
Home Decor' AND classify_corrected == 'Crystal', excluding bau_/meta_test_/BID
camps and SM_CREDIT_LINE_06.

CLI:
  python scripts/v2/home_decor_dashboard_data.py --preset yesterday --out antariksh/home_decor_data.json
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _utils import GRAPH_API, IST, PORTAL_ACCOUNTS, meta_get, meta_paginate, MetaRateLimitError  # noqa: E402

# Reuse the canonical home-decor classification + budget math.
import home_decor_sheet as hd  # noqa: E402

# Ad accounts excluded from all reporting rollups (junk test/leftover camps).
# Mirrors home_decor_sheet's own exclusion; kept local so this module is
# self-contained.
REPORT_EXCLUDE_ACCOUNTS = {"SM_CREDIT_LINE_06"}

from classify_ads import extract_creative_type  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from product_catalogue import derive_category_v2  # noqa: E402

MIN_AD_SPEND = 500          # an ad needs >= this spend to rank as winner/loser
# Portal code -> storefront/website display name (Shopify stores behind each portal).
WEBSITE_NAMES = {"SM": "Studdmuffyn", "SML": "Studdmuffynlife", "NBP": "Nuskhebyparas"}
CREATIVE_TYPES = ["Paras", "Wanda", "Partnership", "AI", "Motion", "Static", "UGC", "Other"]


def creative_type_of(ad_name, campaign_name):
    """Reuse the project classifier, with a UGC overlay.

    For Home Decor the team labels UGC/creator content as 'partnership'
    (e.g. brand_partnership_<creator>_...), so partnership/collab/creator
    ads are reported as UGC. An explicit 'ugc' token also maps to UGC.
    """
    import re as _re
    n = (ad_name or "").lower()
    if _re.search(r'(?:^|_)ugc(?:$|_)', n):
        return "UGC"
    t = extract_creative_type(ad_name, campaign_name)
    if t == "Partnership":
        return "UGC"
    return t


def preset_range(preset):
    """Return (since, until) ISO dates (IST) for a preset string."""
    today = datetime.now(IST).date()
    y = today - timedelta(days=1)
    if preset == "today":
        return today.isoformat(), today.isoformat()
    if preset == "last_7d":
        return (today - timedelta(days=7)).isoformat(), y.isoformat()
    if preset == "last_30d":
        return (today - timedelta(days=30)).isoformat(), y.isoformat()
    # default: yesterday
    return y.isoformat(), y.isoformat()


def _roas(row):
    return hd._extract_roas(row.get("purchase_roas"))


def _orders(row):
    return hd._extract_action(row.get("actions"))


def _revenue(row):
    return hd._extract_action(row.get("action_values"))


def _spend(row):
    try:
        return float(row.get("spend") or 0)
    except (TypeError, ValueError):
        return 0.0


def _parse_meta_time(s):
    """Parse a Meta time string ('2026-05-01T10:20:30+0530') -> aware datetime."""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            return None


def _days_since(s, ref=None):
    """Whole days from a Meta time string until ref (default: now IST). >= 0."""
    dt = _parse_meta_time(s)
    if dt is None:
        return None
    ref = ref or datetime.now(IST)
    return max(0, (ref.astimezone(IST).date() - dt.astimezone(IST).date()).days)


def _days_between(a, b):
    """Whole days between two Meta time strings (b - a), in IST. >= 0 or None."""
    da, db = _parse_meta_time(a), _parse_meta_time(b)
    if da is None or db is None:
        return None
    return max(0, (db.astimezone(IST).date() - da.astimezone(IST).date()).days)


def _closed_day_budget(c):
    """Effective ₹/day a now-paused campaign last carried. per_day_budget only
    counts ACTIVE adsets (always 0 for a paused ABO campaign), so fall back to
    summing ALL adset daily budgets to recover the budget that was switched off."""
    b = hd.per_day_budget(c)
    if b > 0:
        return b
    adsets = ((c.get("adsets") or {}).get("data")) or []
    db = sum(float(a.get("daily_budget") or 0) / 100 for a in adsets)
    if db > 0:
        return db
    return sum(float(a.get("lifetime_budget") or 0) / 100 for a in adsets)


# ── account-level campaign-list cache (fetched once, serves every category) ───
# The list of campaigns per account is NOT date-scoped, so we pull it ONCE per
# effective_status and reuse it across all categories AND all date presets. This
# is what makes building every category affordable in the hourly job: without it
# we'd re-list (and re-deep-fetch) per category and blow the API budget.
_CAMP_LIST_CACHE = {}   # status ("ACTIVE"/"PAUSED") -> [(portal, friendly, campaign_dict)]


def _list_campaigns(status):
    """All non-excluded campaigns for an effective_status, every account, cached."""
    if status in _CAMP_LIST_CACHE:
        return _CAMP_LIST_CACHE[status]
    out = []
    fields = ("id,name,daily_budget,lifetime_budget,start_time,stop_time,"
              "created_time,updated_time,effective_status,"
              "adsets.limit(100){id,name,effective_status,daily_budget,lifetime_budget}")
    for portal, accts in PORTAL_ACCOUNTS.items():
        for env_var, friendly in accts:
            if env_var in REPORT_EXCLUDE_ACCOUNTS:
                continue
            aid = os.environ.get(env_var)
            if not aid:
                continue
            try:
                rows = meta_paginate(
                    f"{GRAPH_API}/{aid}/campaigns",
                    {"fields": fields, "effective_status": f'["{status}"]', "limit": 200})
            except (MetaRateLimitError, Exception) as e:  # noqa: BLE001
                print(f"  x list {friendly} {status}: {e}", file=sys.stderr)
                continue
            for c in rows:
                if not hd.excluded(c.get("name") or ""):
                    out.append((portal, friendly, c))
            time.sleep(0.2)
    _CAMP_LIST_CACHE[status] = out
    return out


def _in_category(name, category):
    """True if a campaign name belongs to `category` per derive_category_v2.
    For Crystal Home Decor we keep the extra product-level 'Crystal' guard that
    drops wanda-misclassified skin/jewellery (matches home_decor_sheet)."""
    if derive_category_v2(name) != category:
        return False
    if category == hd.HOME_DECOR_CATEGORY:
        _, corr_cat = hd.classify_corrected(name)
        if corr_cat != "Crystal":
            return False
    return True


def collect_campaigns(category):
    """[(portal, friendly, campaign_dict)] for active campaigns in `category`."""
    return [(p, f, c) for (p, f, c) in _list_campaigns("ACTIVE")
            if _in_category(c.get("name") or "", category)]


def collect_closed_campaigns(category):
    """[(portal, friendly, campaign_dict)] for PAUSED campaigns in `category`, so
    the dashboard can show budget switched off (with closure date + age)."""
    return [(p, f, c) for (p, f, c) in _list_campaigns("PAUSED")
            if _in_category(c.get("name") or "", category)]


def campaign_insights(cids, since, until):
    """cid -> range metrics dict."""
    out = {}
    field = (f"insights.time_range({{'since':'{since}','until':'{until}'}})"
             f"{{spend,purchase_roas,actions,action_values}}")
    for i in range(0, len(cids), 50):
        batch = cids[i:i + 50]
        d = meta_get(f"{GRAPH_API}/", {"ids": ",".join(batch), "fields": field})
        for cid, obj in (d or {}).items():
            ins = (obj.get("insights") or {}).get("data", []) if isinstance(obj, dict) else []
            row = ins[0] if ins else {}
            out[cid] = {"spend": _spend(row), "roas": _roas(row),
                        "orders": _orders(row), "revenue": _revenue(row)}
        time.sleep(0.3)
    return out


# ── account-level ad performance, cached per preset (serves every category) ───
# Instead of 3 calls per campaign (adset + ad insights + ads list) we pull
# level=ad insights ONCE per account per preset and bucket by campaign/adset/ad.
# Adset and campaign drill-down metrics are summed from these ad rows. The same
# fetched data feeds every category, so cost is ~accounts×presets, not campaigns.
_AD_PERF_CACHE = {}     # preset -> {cid: {asid: {adid: {name,spend,roas,purchases,revenue}}}}


def _ad_perf(preset):
    """{campaign_id: {adset_id: {ad_id: metrics}}} for `preset`, all accounts."""
    if preset in _AD_PERF_CACHE:
        return _AD_PERF_CACHE[preset]
    since, until = preset_range(preset)
    perf = {}
    for portal, accts in PORTAL_ACCOUNTS.items():
        for env_var, friendly in accts:
            if env_var in REPORT_EXCLUDE_ACCOUNTS:
                continue
            aid = os.environ.get(env_var)
            if not aid:
                continue
            try:
                rows = meta_paginate(
                    f"{GRAPH_API}/{aid}/insights",
                    {"level": "ad",
                     "fields": "ad_id,ad_name,adset_id,campaign_id,"
                               "spend,purchase_roas,actions,action_values",
                     "time_range": json.dumps({"since": since, "until": until}),
                     "limit": 500})
            except (MetaRateLimitError, Exception) as e:  # noqa: BLE001
                print(f"  x adperf {friendly} {preset}: {e}", file=sys.stderr)
                continue
            for r in rows:
                cid, asid, adid = r.get("campaign_id"), r.get("adset_id"), r.get("ad_id")
                if not cid or not adid:
                    continue
                perf.setdefault(cid, {}).setdefault(asid, {})[adid] = {
                    "name": r.get("ad_name"),
                    "spend": _spend(r), "roas": _roas(r),
                    "purchases": _orders(r), "revenue": _revenue(r),
                }
            time.sleep(0.3)
    _AD_PERF_CACHE[preset] = perf
    return perf


_CREATIVE_CACHE = {}    # ad_id -> {thumbnail, image, preview, status, name}


def _creatives_for(ad_ids):
    """ad_id -> {thumbnail, image, preview, status, name}, fetched in id-batches,
    cached per ad. `preview` is Meta's shareable ad-preview link (fb.me/...) —
    opens the fully rendered ad so videos play; needs no special permission (the
    video `source` field is permission-gated for this app). `image` is the
    full-res still used as a lightbox fallback for image-only ads."""
    need = [a for a in dict.fromkeys(ad_ids) if a and a not in _CREATIVE_CACHE]
    for i in range(0, len(need), 50):
        batch = need[i:i + 50]
        try:
            d = meta_get(f"{GRAPH_API}/",
                         {"ids": ",".join(batch),
                          "fields": "name,effective_status,preview_shareable_link,"
                                    "creative{thumbnail_url,image_url}"})
        except (MetaRateLimitError, Exception):  # noqa: BLE001
            d = {}
        for adid, obj in (d or {}).items():
            if not isinstance(obj, dict):
                continue
            cr = obj.get("creative") or {}
            _CREATIVE_CACHE[adid] = {
                "thumbnail": cr.get("thumbnail_url") or cr.get("image_url"),
                "image": cr.get("image_url") or cr.get("thumbnail_url"),
                "preview": obj.get("preview_shareable_link"),
                "status": obj.get("effective_status"),
                "name": obj.get("name"),
            }
        time.sleep(0.2)
    return {a: _CREATIVE_CACHE.get(a, {}) for a in ad_ids}


def _roll(d):
    """Finalize a {spend,revenue,purchases,count,...} accumulator into output."""
    sp = d.get("spend", 0.0)
    return {
        "count": d.get("count", 0),
        "spend": round(sp),
        "revenue": round(d.get("revenue", 0.0)),
        "purchases": int(d.get("purchases", 0)),
        "roas": round((d.get("revenue", 0.0) / sp), 2) if sp else 0.0,
    }


def build_creative_report(all_ads):
    """Winners/losers + per-creative-type slices + scaling recommendations."""
    from collections import defaultdict
    ranked = [a for a in all_ads if a["spend"] >= MIN_AD_SPEND]
    winning = sorted(ranked, key=lambda a: -a["roas"])[:12]
    losing = sorted(ranked, key=lambda a: a["roas"])[:12]

    by_type = defaultdict(lambda: {"spend": 0.0, "revenue": 0.0, "purchases": 0, "count": 0})
    by_prod_type = defaultdict(lambda: defaultdict(lambda: {"spend": 0.0, "revenue": 0.0, "purchases": 0, "count": 0}))
    for a in all_ads:
        t = a["type"] if a["type"] in CREATIVE_TYPES else "Other"
        for bucket in (by_type[t], by_prod_type[a["product"]][t]):
            bucket["spend"] += a["spend"]; bucket["revenue"] += a["revenue"]
            bucket["purchases"] += a["purchases"]; bucket["count"] += 1

    by_type_out = {t: _roll(by_type[t]) for t in CREATIVE_TYPES if by_type[t]["count"]}

    # Scaling recommendations per product
    scaling = []
    for prod, types in by_prod_type.items():
        spend = sum(v["spend"] for v in types.values())
        rev = sum(v["revenue"] for v in types.values())
        roas = round((rev / spend), 2) if spend else 0.0
        present = {t for t, v in types.items() if v["count"] > 0}
        missing = [t for t in ("Paras", "Motion", "Static", "UGC") if t not in present]
        if roas >= 2.5:
            rec = "Scale — winning ROAS; add fresh variants of top format"
        elif roas >= 1.5:
            rec = "Maintain — test new angles to push ROAS up"
        else:
            rec = "Fix or cut — ROAS below target"
        if missing:
            rec += " · missing: " + ", ".join(missing)
        scaling.append({"product": prod, "roas": roas, "spend": round(spend),
                        "ad_count": sum(v["count"] for v in types.values()),
                        "missing": missing, "recommendation": rec})
    scaling.sort(key=lambda x: -x["spend"])

    def slim(a):
        return {k: a.get(k) for k in ("id", "name", "product", "type", "thumbnail",
                                      "image", "preview", "spend", "roas", "purchases",
                                      "campaign")}
    return {
        "min_spend": MIN_AD_SPEND,
        "winning": [slim(a) for a in winning],
        "losing": [slim(a) for a in losing],
        "by_type": by_type_out,
        "paras": by_type_out.get("Paras", _roll({})),
        "motion_still": {"Motion": by_type_out.get("Motion", _roll({})),
                         "Static": by_type_out.get("Static", _roll({}))},
        "ugc": by_type_out.get("UGC", _roll({})),
        "scaling": scaling,
    }


def build(preset="yesterday", category=None, with_creatives=True):
    from collections import defaultdict
    category = category or hd.HOME_DECOR_CATEGORY
    since, until = preset_range(preset)
    _now = datetime.now(IST)
    fetched_at = _now.strftime("%d %b %Y, %H:%M:%S IST")
    fetched_ts = _now.timestamp()          # epoch seconds, for JS "x ago"
    rows = collect_campaigns(category)
    cids = [c["id"] for _, _, c in rows]
    cins = campaign_insights(cids, since, until) if cids else {}
    # Account-level ad performance (cached, shared across categories) replaces the
    # old 3-calls-per-campaign drill-down. Adset/ad metrics are summed from here.
    perf = _ad_perf(preset) if with_creatives else {}
    ad_ids = set()
    for _, _, c in rows:
        for ads in (perf.get(c["id"]) or {}).values():
            ad_ids.update(ads.keys())
    creatives = _creatives_for(ad_ids) if (with_creatives and ad_ids) else {}

    campaigns = []
    all_ads = []
    prod_roll = defaultdict(lambda: {"budget": 0.0, "spend": 0.0, "revenue": 0.0,
                                     "orders": 0.0, "campaigns": 0, "adsets": 0,
                                     "days_running": None})
    portal_roll = defaultdict(lambda: {"budget": 0.0, "spend": 0.0, "revenue": 0.0,
                                       "orders": 0.0, "campaigns": 0, "adsets": 0})
    tot = {"budget": 0.0, "spend": 0.0, "orders": 0.0, "revenue": 0.0, "adsets": 0}
    for portal, friendly, c in rows:
        cid = c["id"]
        name = c.get("name") or ""
        product, _ = hd.classify_corrected(name)
        m = cins.get(cid, {})
        budget = hd.per_day_budget(c)
        adsets_meta = ((c.get("adsets") or {}).get("data")) or []
        cperf = perf.get(cid, {})          # adset_id -> {ad_id: metrics}
        adsets = []
        for a in adsets_meta:
            asid = a.get("id")
            ads = []
            a_spend = a_rev = 0.0
            a_pur = 0
            for adid, pm in (cperf.get(asid) or {}).items():
                cr = creatives.get(adid, {})
                ad_name = pm.get("name") or cr.get("name")
                ctype = creative_type_of(ad_name, name)
                ad_obj = {"id": adid, "name": ad_name,
                          "status": cr.get("status"),
                          "thumbnail": cr.get("thumbnail"),
                          "image": cr.get("image"),
                          "preview": cr.get("preview"),
                          "type": ctype,
                          "spend": round(pm.get("spend", 0.0)),
                          "roas": round(pm.get("roas", 0.0), 2),
                          "purchases": int(pm.get("purchases", 0))}
                ads.append(ad_obj)
                all_ads.append({**ad_obj, "product": product, "campaign": name,
                                "revenue": pm.get("revenue", 0.0)})
                a_spend += pm.get("spend", 0.0)
                a_rev += pm.get("revenue", 0.0)
                a_pur += int(pm.get("purchases", 0))
            ads.sort(key=lambda x: -x["spend"])
            adsets.append({
                "id": asid, "name": a.get("name"),
                "status": a.get("effective_status"),
                "roas": round((a_rev / a_spend), 2) if a_spend else 0.0,
                "purchases": a_pur,
                "spend": round(a_spend),
                "ads": ads,
            })
        adsets.sort(key=lambda x: -x["spend"])
        campaigns.append({
            "id": cid, "name": name, "product": product,
            "portal": portal, "account": friendly,
            "budget": round(budget), "adset_count": len(adsets_meta),
            "spend": round(m.get("spend", 0.0)), "roas": round(m.get("roas", 0.0), 2),
            "orders": int(m.get("orders", 0)), "revenue": round(m.get("revenue", 0.0)),
            "created_time": c.get("created_time"),
            "days_running": _days_since(c.get("created_time")),
            "adsets": adsets,
        })
        pr = prod_roll[product]
        pr["budget"] += budget; pr["spend"] += m.get("spend", 0.0)
        pr["revenue"] += m.get("revenue", 0.0); pr["orders"] += m.get("orders", 0.0)
        pr["campaigns"] += 1; pr["adsets"] += len(adsets_meta)
        c_days = _days_since(c.get("created_time"))   # product age = oldest live campaign
        if c_days is not None:
            pr["days_running"] = c_days if pr["days_running"] is None else max(pr["days_running"], c_days)
        qr = portal_roll[portal]
        qr["budget"] += budget; qr["spend"] += m.get("spend", 0.0)
        qr["revenue"] += m.get("revenue", 0.0); qr["orders"] += m.get("orders", 0.0)
        qr["campaigns"] += 1; qr["adsets"] += len(adsets_meta)
        tot["budget"] += budget; tot["spend"] += m.get("spend", 0.0)
        tot["orders"] += m.get("orders", 0.0); tot["revenue"] += m.get("revenue", 0.0)
        tot["adsets"] += len(adsets_meta)

    campaigns.sort(key=lambda x: -x["budget"])
    total_budget = tot["budget"] or 1
    products = []
    for prod, v in prod_roll.items():
        products.append({
            "product": prod, "budget": round(v["budget"]),
            "budget_share": round(100 * v["budget"] / total_budget, 1),
            "spend": round(v["spend"]), "revenue": round(v["revenue"]),
            "orders": int(v["orders"]), "campaigns": v["campaigns"],
            "adsets": v["adsets"], "days_running": v["days_running"],
            "roas": round((v["revenue"] / v["spend"]), 2) if v["spend"] else 0.0,
        })
    products.sort(key=lambda x: -x["budget"])

    websites = []
    for code in ("SM", "SML", "NBP"):
        v = portal_roll.get(code, {})
        sp = v.get("spend", 0.0)
        websites.append({
            "portal": code, "name": WEBSITE_NAMES[code],
            "budget": round(v.get("budget", 0.0)), "spend": round(sp),
            "revenue": round(v.get("revenue", 0.0)), "orders": int(v.get("orders", 0)),
            "campaigns": v.get("campaigns", 0),
            "roas": round((v.get("revenue", 0.0) / sp), 2) if sp else 0.0,
        })

    # ── Closed budget: category campaigns paused within [since, until] ──
    # A campaign closed mid-window already drops out of `campaigns` (those are
    # ACTIVE-only), so it is naturally "shifted" here. "Closed on" uses Meta's
    # updated_time (the closest available proxy for when it was switched off).
    closed = []
    closed_rows = collect_closed_campaigns(category)
    in_window = []
    for portal, friendly, c in closed_rows:
        upd = _parse_meta_time(c.get("updated_time"))
        if upd is None:
            continue
        d_iso = upd.astimezone(IST).date().isoformat()
        if since <= d_iso <= until:
            in_window.append((portal, friendly, c, d_iso))
    cl_ins = campaign_insights([c["id"] for _, _, c, _ in in_window],
                               since, until) if in_window else {}
    for portal, friendly, c, d_iso in in_window:
        name = c.get("name") or ""
        product, _ = hd.classify_corrected(name)
        mi = cl_ins.get(c["id"], {})
        closed.append({
            "id": c["id"], "name": name, "product": product,
            "portal": portal, "account": friendly,
            "budget": round(_closed_day_budget(c)),
            "closed_on": d_iso,
            "created_time": c.get("created_time"),
            "age_days": _days_between(c.get("created_time"), c.get("updated_time")),
            "spend": round(mi.get("spend", 0.0)),
            "roas": round(mi.get("roas", 0.0), 2),
            "orders": int(mi.get("orders", 0)),
        })
    closed.sort(key=lambda x: (x["closed_on"], x["budget"]), reverse=True)
    closed_budget_total = round(sum(x["budget"] for x in closed))

    blended_roas = (tot["revenue"] / tot["spend"]) if tot["spend"] else 0.0
    overview = {
        "budget": round(tot["budget"]), "spend": round(tot["spend"]),
        "orders": int(tot["orders"]), "revenue": round(tot["revenue"]),
        "roas": round(blended_roas, 2),
        "campaigns": len(campaigns), "adsets": tot["adsets"],
        "products": len(products), "ads": len(all_ads),
        "closed": len(closed), "closed_budget": closed_budget_total,
    }
    return {"ok": True, "preset": preset, "category": category,
            "since": since, "until": until,
            "fetched_at": fetched_at, "fetched_ts": fetched_ts,
            "overview": overview, "websites": websites, "products": products,
            "campaigns": campaigns, "closed": closed,
            "creative_report": build_creative_report(all_ads)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset", default="yesterday",
                    choices=["today", "yesterday", "last_7d", "last_30d"])
    ap.add_argument("--category", default=None,
                    help="category to build (default: Crystal Home Decor)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-creatives", action="store_true")
    args = ap.parse_args()
    if not os.getenv("META_ACCESS_TOKEN"):
        sys.exit("META_ACCESS_TOKEN not set")
    data = build(args.preset, category=args.category,
                 with_creatives=not args.no_creatives)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
        ov = data["overview"]
        print(f"Wrote {args.out}: {ov['campaigns']} camps, {ov['adsets']} adsets, "
              f"Rs {ov['budget']:,}/day budget, Rs {ov['spend']:,} spend ({args.preset})")
    else:
        print(payload)


if __name__ == "__main__":
    main()
