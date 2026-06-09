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
from _utils import GRAPH_API, IST, PORTAL_ACCOUNTS, meta_get, MetaRateLimitError  # noqa: E402

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


def collect_campaigns():
    """[(portal, friendly, campaign_dict)] for active home-decor campaigns."""
    out = []
    for portal, accts in PORTAL_ACCOUNTS.items():
        for env_var, friendly in accts:
            if env_var in REPORT_EXCLUDE_ACCOUNTS:
                continue
            aid = os.environ.get(env_var)
            if not aid:
                continue
            try:
                d = meta_get(
                    f"{GRAPH_API}/{aid}/campaigns",
                    {"fields": "id,name,daily_budget,lifetime_budget,start_time,stop_time,"
                               "adsets.limit(100){id,name,effective_status,daily_budget,lifetime_budget}",
                     "effective_status": '["ACTIVE"]', "limit": 500},
                    max_retries=3,
                )
            except (MetaRateLimitError, Exception) as e:  # noqa: BLE001
                print(f"  x {friendly}: {e}", file=sys.stderr)
                continue
            for c in (d or {}).get("data") or []:
                name = c.get("name") or ""
                if hd.excluded(name):
                    continue
                if derive_category_v2(name) != hd.HOME_DECOR_CATEGORY:
                    continue
                _, corr_cat = hd.classify_corrected(name)
                if corr_cat != "Crystal":
                    continue
                out.append((portal, friendly, c))
            time.sleep(0.2)
    return out


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


def adset_insights(cid, since, until):
    """adset_id -> {spend, roas, purchases, revenue} for one campaign."""
    out = {}
    try:
        d = meta_get(f"{GRAPH_API}/{cid}/insights",
                     {"level": "adset",
                      "fields": "adset_id,adset_name,spend,purchase_roas,actions,action_values",
                      "time_range": json.dumps({"since": since, "until": until}),
                      "limit": 200})
    except (MetaRateLimitError, Exception):  # noqa: BLE001
        return out
    for row in (d or {}).get("data") or []:
        out[row.get("adset_id")] = {
            "spend": _spend(row), "roas": _roas(row),
            "purchases": _orders(row), "revenue": _revenue(row),
        }
    return out


def ad_insights(cid, since, until):
    """ad_id -> {spend, roas, purchases, revenue} for one campaign."""
    out = {}
    try:
        d = meta_get(f"{GRAPH_API}/{cid}/insights",
                     {"level": "ad",
                      "fields": "ad_id,spend,purchase_roas,actions,action_values",
                      "time_range": json.dumps({"since": since, "until": until}),
                      "limit": 300})
    except (MetaRateLimitError, Exception):  # noqa: BLE001
        return out
    for row in (d or {}).get("data") or []:
        out[row.get("ad_id")] = {
            "spend": _spend(row), "roas": _roas(row),
            "purchases": _orders(row), "revenue": _revenue(row),
        }
    return out


def campaign_ads(cid):
    """adset_id -> [ {id,name,status,thumbnail,image,preview} ] for one campaign.
    `preview` is Meta's shareable ad-preview link (fb.me/...) — opens the fully
    rendered ad so videos play; needs no token and no special permission (the
    video `source` field is permission-gated for this app). `image` is the
    full-res still used as a lightbox fallback for image-only ads."""
    out = {}
    try:
        d = meta_get(f"{GRAPH_API}/{cid}/ads",
                     {"fields": "id,name,adset_id,effective_status,"
                                "preview_shareable_link,creative{thumbnail_url,image_url}",
                      "limit": 200})
    except (MetaRateLimitError, Exception):  # noqa: BLE001
        return out
    for ad in (d or {}).get("data") or []:
        cr = ad.get("creative") or {}
        out.setdefault(ad.get("adset_id"), []).append({
            "id": ad.get("id"),
            "name": ad.get("name"),
            "status": ad.get("effective_status"),
            "thumbnail": cr.get("thumbnail_url") or cr.get("image_url"),
            "image": cr.get("image_url") or cr.get("thumbnail_url"),
            "preview": ad.get("preview_shareable_link"),
        })
    return out


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


def build(preset="yesterday", with_creatives=True):
    from collections import defaultdict
    since, until = preset_range(preset)
    _now = datetime.now(IST)
    fetched_at = _now.strftime("%d %b %Y, %H:%M:%S IST")
    fetched_ts = _now.timestamp()          # epoch seconds, for JS "x ago"
    rows = collect_campaigns()
    cids = [c["id"] for _, _, c in rows]
    cins = campaign_insights(cids, since, until) if cids else {}

    campaigns = []
    all_ads = []
    prod_roll = defaultdict(lambda: {"budget": 0.0, "spend": 0.0, "revenue": 0.0,
                                     "orders": 0.0, "campaigns": 0, "adsets": 0})
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
        ains = adset_insights(cid, since, until)
        ads_by_set = campaign_ads(cid) if with_creatives else {}
        adins = ad_insights(cid, since, until) if with_creatives else {}
        adsets = []
        for a in adsets_meta:
            asid = a.get("id")
            am = ains.get(asid, {})
            ads = []
            for ad in ads_by_set.get(asid, []):
                pm = adins.get(ad["id"], {})
                ctype = creative_type_of(ad.get("name"), name)
                ad_obj = {**ad, "type": ctype,
                          "spend": round(pm.get("spend", 0.0)),
                          "roas": round(pm.get("roas", 0.0), 2),
                          "purchases": int(pm.get("purchases", 0))}
                ads.append(ad_obj)
                all_ads.append({**ad_obj, "product": product, "campaign": name,
                                "revenue": pm.get("revenue", 0.0)})
            adsets.append({
                "id": asid, "name": a.get("name"),
                "status": a.get("effective_status"),
                "roas": round(am.get("roas", 0.0), 2),
                "purchases": int(am.get("purchases", 0)),
                "spend": round(am.get("spend", 0.0)),
                "ads": ads,
            })
        campaigns.append({
            "id": cid, "name": name, "product": product,
            "portal": portal, "account": friendly,
            "budget": round(budget), "adset_count": len(adsets_meta),
            "spend": round(m.get("spend", 0.0)), "roas": round(m.get("roas", 0.0), 2),
            "orders": int(m.get("orders", 0)), "revenue": round(m.get("revenue", 0.0)),
            "adsets": adsets,
        })
        pr = prod_roll[product]
        pr["budget"] += budget; pr["spend"] += m.get("spend", 0.0)
        pr["revenue"] += m.get("revenue", 0.0); pr["orders"] += m.get("orders", 0.0)
        pr["campaigns"] += 1; pr["adsets"] += len(adsets_meta)
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
            "adsets": v["adsets"],
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

    blended_roas = (tot["revenue"] / tot["spend"]) if tot["spend"] else 0.0
    overview = {
        "budget": round(tot["budget"]), "spend": round(tot["spend"]),
        "orders": int(tot["orders"]), "revenue": round(tot["revenue"]),
        "roas": round(blended_roas, 2),
        "campaigns": len(campaigns), "adsets": tot["adsets"],
        "products": len(products), "ads": len(all_ads),
    }
    return {"ok": True, "preset": preset, "since": since, "until": until,
            "fetched_at": fetched_at, "fetched_ts": fetched_ts,
            "overview": overview, "websites": websites, "products": products,
            "campaigns": campaigns, "creative_report": build_creative_report(all_ads)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset", default="yesterday",
                    choices=["today", "yesterday", "last_7d", "last_30d"])
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-creatives", action="store_true")
    args = ap.parse_args()
    if not os.getenv("META_ACCESS_TOKEN"):
        sys.exit("META_ACCESS_TOKEN not set")
    data = build(args.preset, with_creatives=not args.no_creatives)
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
