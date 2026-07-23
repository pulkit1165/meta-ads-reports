#!/usr/bin/env python3
"""
Creative Elimination Radar — per product, which creative types / combinations to
KEEP vs CUT based on their last N attempts.

Objective (operator's words): "eliminate creative types of products according to
their last 10 attempts — e.g. for Hair Mist, of the last 10 campaigns, which
performed above 2 ROAS and what was their creative type & combination, and which
did not." For every product, mapped to its NTN SKU.

How it works
------------
1. Map each ad row -> product via the repo's keyword catalogue
   (classify_corrected / derive_product_and_category). The `ntn_` prefix in
   campaign names is just the brand tag, NOT a SKU, so product identity comes
   from the keyword slug (phusphus -> "Phus Phus Hair Mist").
2. Tag each ad with:
     - creative_type  : Paras / Wanda / Static / Motion / AI / Partnership / Other
     - storyline      : st-code messaging angle (sparse — ~17% of names)
   "Combination" = (creative_type x storyline).
3. Aggregate to two grains:
     - CAMPAIGN : sum spend/revenue over the campaign's life -> blended ROAS,
                  first_seen date, dominant creative_type & storyline (by spend).
     - AD       : same, per ad creative.
4. Score: an "attempt" must have spent >= --min-spend. A WIN is blended
   ROAS >= --threshold (default 2.0). For each product x creative_type over the
   last --last-n campaign attempts: win-rate + blended ROAS -> KEEP / ELIMINATE.
5. SKU crosswalk: product display name -> NTN code (explicit NTN in name first,
   then fuzzy match to product_ntn_labels). Unmatched products are flagged.

Outputs (CSV always; Google Sheet tabs if --sheet-id given):
  - verdicts          : one row per product x creative_type with KEEP/CUT
  - last10_campaigns  : per product, its last N campaign attempts
  - combination_detail: per product x (type x storyline) ad-level rollup
  - sku_gaps          : product -> NTN crosswalk, unmatched flagged

Usage:
  python3 scripts/v2/creative_elimination.py --db state/ntn.db
  python3 scripts/v2/creative_elimination.py --db /tmp/ntn.db --sheet-id <ID>
"""
import argparse
import csv
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO / "scripts"))
sys.path.insert(0, str(_REPO / "scripts" / "v2"))

import re  # noqa: E402

from classify_ads import (extract_creative_type, extract_sentiment,  # noqa: E402
                          extract_ntn_code, extract_product_slug)
from active_budget_by_product import classify_corrected  # noqa: E402  (wanda/astro-corrected)

# Tokens to strip from a recovered product slug: NTN codes, storyline codes,
# funnel/audience/dup boilerplate, dates. What remains is the product name.
_SLUG_DROP = re.compile(
    r'^(ntn\d+|st\d+[a-f]?|\d+|dup|exc|copy|sm|sml|smsk|smskin|adv|web|conv|'
    r'rtg|retarget|sales?|reel|clp|mix|180dp|180dpsm|180imp|60dp|7d|inde|'
    r'paras|wanda|ant|v\d+|creatives?|download|call|video|dimp|\d+dimp|master|'
    r'dv|app|gateway|camp|campaign|trishul|motion|static|partnership|st|ugc|'
    r'installs?|boosterkit|trf|gold|aarjav|oooh|combo|june|\d+(st|nd|rd|th))$')

# Products that are intentionally not real single SKUs — never emit verdicts for
# these (they are catch-all buckets in the catalogue).
SKIP_PRODUCTS = {"Unmapped", "Mix/Multiple", "Multiple Products"}

# The catalogue lumps everything into one "Jewellery" bucket, but the operator
# runs distinct jewellery products (velore chain, rice chain, pendant, …). These
# sub-product tokens live in the AD name. First match wins; velore/rice are
# checked before the generic 'chain' so "velore chain" -> Velore Chain.
JEWELLERY_SUBPRODUCTS = [
    ("Velore Chain", ["velore"]),
    ("Rice Chain",   ["rice"]),
    ("Pendant",      ["pendant", "locket"]),
    ("Chain",        ["chain", "cuban", "rope", "figaro", "snake_chain", "box_chain"]),
    ("Mangalsutra",  ["mangalsutra"]),
    ("Bracelet",     ["bracelet", "kada"]),
    ("Ring",         ["ring"]),
    ("Earrings",     ["earring", "stud", "hoop", "jhumka"]),
    ("Anklet",       ["anklet", "payal"]),
    ("Nazar / Evil Eye", ["nazar", "evil_eye", "evil eye"]),
]


def jewellery_subproduct(ad_name, campaign_name):
    """Refine the generic 'Jewellery' bucket to a specific product using the ad +
    campaign text. Falls back to 'Jewellery (unspecified)' when no token hits."""
    t = f"{ad_name or ''} {campaign_name or ''}".lower()
    for label, toks in JEWELLERY_SUBPRODUCTS:
        if any(k in t for k in toks):
            return label
    return "Jewellery (unspecified)"


def _clean_slug(slug):
    """Turn a raw extract_product_slug into a display name by dropping NTN/st/
    funnel/date noise tokens. Returns a Title-Cased name or None if nothing real
    is left."""
    if not slug:
        return None
    slug = slug.lstrip("~ ")  # extract_product_slug marks heuristics with '~'
    keep = []
    for tok in slug.split("_"):
        if not tok or _SLUG_DROP.match(tok):       # drop noise on the raw token
            continue
        tok = re.sub(r"\d+$", "", tok)             # then strip trailing digits
        if tok and not _SLUG_DROP.match(tok):
            keep.append(tok)
    if not keep:
        return None
    name = " ".join(keep).replace("/", " ").strip()
    return name.title() if name and len(name) > 2 else None


def recover_unmapped(ad_name, campaign_name, ntn_map):
    """Try to name an 'Unmapped' campaign: NTN code -> real SKU product first
    (most reliable, also gives a SKU), else a cleaned keyword slug. Returns
    (product, category) or None to keep it Unmapped."""
    code = extract_ntn_code(ad_name or "") or extract_ntn_code(campaign_name or "")
    if code and code in ntn_map:
        name, cat = ntn_map[code]
        return (name[:48], cat)
    slug = (_clean_slug(extract_product_slug(campaign_name or ""))
            or _clean_slug(extract_product_slug(ad_name or "")))
    if slug:
        return (slug, "Unmapped*")
    return None


# ── load + classify ──────────────────────────────────────────────────────────
def load_rows(db_path, days=None):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cutoff = con.execute("SELECT MAX(date) FROM meta_ads_daily").fetchone()[0]
    start = None
    sql = ("""SELECT date, portal, campaign_id, campaign_name, ad_id, ad_name,
                     spend, revenue
              FROM meta_ads_daily""")
    if days:
        start = (date.fromisoformat(cutoff) - timedelta(days=days - 1)).isoformat()
        rows = con.execute(sql + " WHERE date >= ?", (start,)).fetchall()
    else:
        rows = con.execute(sql).fetchall()
    con.close()
    return rows, cutoff, start


def creative_type_v2(ad_name, campaign_name):
    """'Wanda' is the operator's AI-generator team, NOT a creative format. When a
    name resolves to Wanda, strip the wanda tag and re-read the real format
    (Motion / Static / Partnership / AI / Paras) from the ad text; fall back to
    'Other' only if no format keyword survives."""
    ct = extract_creative_type(ad_name, campaign_name)
    if ct == "Wanda":
        a = (ad_name or "").lower().replace("wanda", "")
        c = (campaign_name or "").lower().replace("wanda", "")
        ct2 = extract_creative_type(a, c)
        ct = ct2 if ct2 != "Wanda" else "Other"
    return ct


def aggregate(rows, ntn_map=None):
    """Return (campaigns, ads) dicts keyed by id, each carrying product/type/
    storyline, spend, revenue, first/last date. Dominant type & storyline are
    chosen by spend so the campaign's label reflects where the money went."""
    ntn_map = ntn_map or {}
    camps = {}
    ads = {}
    for r in rows:
        name_ad = r["ad_name"] or ""
        name_cp = r["campaign_name"] or ""
        product, category = classify_corrected(name_cp or name_ad)
        if product == "Jewellery":
            product = jewellery_subproduct(name_ad, name_cp)
        elif product == "Unmapped":
            rec = recover_unmapped(name_ad, name_cp, ntn_map)
            if rec:
                product, category = rec
        ctype = creative_type_v2(name_ad, name_cp)
        story = extract_sentiment(name_ad) or extract_sentiment(name_cp) or "—"
        sp = r["spend"] or 0.0
        rev = r["revenue"] or 0.0

        c = camps.get(r["campaign_id"])
        if c is None:
            c = camps[r["campaign_id"]] = {
                "name": name_cp, "product": product, "category": category,
                "portal": r["portal"], "first": r["date"], "last": r["date"],
                "spend": 0.0, "rev": 0.0,
                "ct_spend": defaultdict(float), "st_spend": defaultdict(float),
            }
        c["spend"] += sp
        c["rev"] += rev
        c["first"] = min(c["first"], r["date"])
        c["last"] = max(c["last"], r["date"])
        c["ct_spend"][ctype] += sp
        c["st_spend"][story] += sp

        a = ads.get(r["ad_id"])
        if a is None:
            a = ads[r["ad_id"]] = {
                "name": name_ad, "product": product, "category": category,
                "portal": r["portal"], "campaign_id": r["campaign_id"],
                "ctype": ctype, "story": story,
                "first": r["date"], "last": r["date"], "spend": 0.0, "rev": 0.0,
            }
        a["spend"] += sp
        a["rev"] += rev
        a["first"] = min(a["first"], r["date"])
        a["last"] = max(a["last"], r["date"])
    return camps, ads


def _dominant(d):
    return max(d.items(), key=lambda kv: kv[1])[0] if d else "—"


def finalize(camps, ads):
    for c in camps.values():
        c["roas"] = c["rev"] / c["spend"] if c["spend"] else 0.0
        c["ctype"] = _dominant(c["ct_spend"])
        c["story"] = _dominant(c["st_spend"])
    for a in ads.values():
        a["roas"] = a["rev"] / a["spend"] if a["spend"] else 0.0
    return camps, ads


# ── SKU crosswalk: product display name -> NTN code ──────────────────────────
def build_sku_crosswalk(db_path, rows, products):
    """Map each product display name to an NTN code.
       1. explicit NTN### in the ad/campaign names of that product (voted)
       2. fuzzy token-overlap against product_ntn_labels.product
       Returns {product: (ntn_code or '', match_method, label_name or '')}."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    labels = con.execute(
        "SELECT ntn_code, product, category FROM product_ntn_labels"
    ).fetchall()
    con.close()

    # 1. explicit NTN votes per product
    explicit = defaultdict(lambda: defaultdict(int))
    for r in rows:
        code = extract_ntn_code(r["ad_name"] or "") or extract_ntn_code(r["campaign_name"] or "")
        if not code:
            continue
        product, _ = classify_corrected(r["campaign_name"] or r["ad_name"] or "")
        explicit[product][code] += 1

    # label lookup tables for fuzzy match
    label_tokens = []
    for lb in labels:
        toks = set(re.findall(r"[a-z0-9]+", (lb["product"] or "").lower()))
        toks = {t for t in toks if len(t) > 2}
        label_tokens.append((lb["ntn_code"], lb["product"], toks))

    STOP = {"the", "and", "with", "for", "ml", "combo", "kit", "pack",
            "set", "of", "crystal", "skin", "hair"}

    out = {}
    for product in products:
        if product in SKIP_PRODUCTS:
            out[product] = ("", "skip", "")
            continue
        # explicit wins
        if explicit.get(product):
            code = max(explicit[product].items(), key=lambda kv: kv[1])[0]
            lbl = next((lb["product"] for lb in labels if lb["ntn_code"] == code), "")
            out[product] = (code, "explicit", lbl)
            continue
        # fuzzy: best token overlap (minus stopwords)
        ptoks = set(re.findall(r"[a-z0-9]+", product.lower()))
        ptoks = {t for t in ptoks if len(t) > 2 and t not in STOP}
        best = ("", 0, "")
        for code, lname, toks in label_tokens:
            inter = ptoks & (toks - STOP)
            score = len(inter)
            if score > best[1]:
                best = (code, score, lname)
        if best[1] >= 2:  # need >=2 meaningful shared tokens
            out[product] = (best[0], f"fuzzy({best[1]})", best[2])
        else:
            out[product] = ("", "UNMATCHED", "")
    return out


# ── scoring / verdicts ───────────────────────────────────────────────────────
def product_campaigns(camps, min_spend):
    """product -> list of campaign dicts (attempt: spend>=min_spend), newest first."""
    by = defaultdict(list)
    for c in camps.values():
        if c["product"] in SKIP_PRODUCTS:
            continue
        if c["spend"] >= min_spend:
            by[c["product"]].append(c)
    for p in by:
        by[p].sort(key=lambda c: c["first"], reverse=True)
    return by


def verdicts(by_product, last_n, threshold, min_attempts_cut=3, lag=0.85):
    """One row per product x creative_type over its last_n campaign attempts.

    Verdict is RELATIVE within the product (most spend is one type, so a flat
    ROAS bar would just eliminate the whole catalogue). Each type is judged
    against the product's own blended ROAS:
      SCALE  — blended ROAS >= threshold (a genuine winner; do more)
      KEEP   — at/above the product's own average (below threshold though)
      CUT    — >= min_attempts_cut attempts AND blended ROAS clearly below the
               product average (< lag x avg). Proven laggard for this product.
      TEST   — too few attempts (< min_attempts_cut) to judge; keep testing.
    """
    rows = []
    for product, camps in by_product.items():
        last = camps[:last_n]
        p_sp = sum(c["spend"] for c in last)
        p_br = sum(c["rev"] for c in last) / p_sp if p_sp else 0.0
        bytype = defaultdict(list)
        for c in last:
            bytype[c["ctype"]].append(c)
        allbytype = defaultdict(list)
        for c in camps:
            allbytype[c["ctype"]].append(c)
        # rank types within product by blended ROAS
        type_br = {}
        for ctype, cs in bytype.items():
            sp = sum(c["spend"] for c in cs)
            type_br[ctype] = sum(c["rev"] for c in cs) / sp if sp else 0.0
        ranking = sorted(type_br, key=type_br.get, reverse=True)
        for ctype, cs in sorted(bytype.items(), key=lambda kv: -sum(c["spend"] for c in kv[1])):
            n = len(cs)
            wins = sum(1 for c in cs if c["roas"] >= threshold)
            sp = sum(c["spend"] for c in cs)
            br = sum(c["rev"] for c in cs) / sp if sp else 0.0
            winrate = wins / n if n else 0.0
            ah = allbytype[ctype]
            ah_sp = sum(c["spend"] for c in ah)
            ah_br = sum(c["rev"] for c in ah) / ah_sp if ah_sp else 0.0
            if br >= threshold:
                verdict = "SCALE"
            elif n < min_attempts_cut:
                verdict = "TEST"
            elif br < p_br * lag:
                verdict = "CUT"
            elif br >= p_br:
                verdict = "KEEP"
            else:
                verdict = "WATCH"
            st_sp = defaultdict(float)
            for c in cs:
                st_sp[c["story"]] += c["spend"]
            top_story = _dominant(st_sp)
            rows.append({
                "product": product, "category": cs[0]["category"],
                "creative_type": ctype, "top_storyline": top_story,
                "rank_in_product": ranking.index(ctype) + 1,
                "attempts": n, "wins": wins, "win_rate": round(winrate, 2),
                "blended_roas": round(br, 2), "product_avg_roas": round(p_br, 2),
                "spend": round(sp),
                "all_attempts": len(ah), "all_blended_roas": round(ah_br, 2),
                "verdict": verdict,
            })
    return rows


def last10_rows(by_product, last_n, threshold):
    rows = []
    for product, camps in by_product.items():
        for i, c in enumerate(camps[:last_n], 1):
            rows.append({
                "product": product, "category": c["category"], "n": i,
                "started": c["first"], "ended": c["last"], "portal": c["portal"],
                "creative_type": c["ctype"], "storyline": c["story"],
                "spend": round(c["spend"]), "roas": round(c["roas"], 2),
                "result": "WIN" if c["roas"] >= threshold else "lose",
                "campaign": c["name"][:70],
            })
    return rows


def all_campaign_rows(camps, threshold, min_spend=500.0):
    """Every campaign across every product (incl. Unmapped/Mix) with spend above
    a small floor — one row each, creative type + storyline + ROAS shown. The
    full detail dump. Newest campaign first within each product."""
    rows = []
    for cid, c in camps.items():
        if c["spend"] < min_spend:
            continue
        win = c["roas"] >= threshold
        rows.append({
            "product": c["product"], "category": c["category"],
            "campaign_id": str(cid),  # str so Sheets keeps the full 17-18 digits
            "campaign": c["name"][:80], "creative_type": c["ctype"],
            "storyline": c["story"], "portal": c["portal"],
            "started": c["first"], "last_active": c["last"],
            "spend": round(c["spend"]), "revenue": round(c["rev"]),
            "roas": round(c["roas"], 2),
            "result": "WIN" if win else "lose",
            "verdict": "SCALE" if win else "WEAK",   # for result-cell colour
        })
    rows.sort(key=lambda r: r["started"], reverse=True)       # newest first ...
    rows.sort(key=lambda r: (r["category"], r["product"]))    # ... within product
    return rows


def combination_rows(ads, min_spend_ad, threshold):
    """product x (creative_type x storyline) ad-level rollup, with the ad count
    split into ROAS bands (<1 / 1-2 / 2-3 / 3+) so the distribution is visible."""
    cells = defaultdict(lambda: {"n": 0, "wins": 0, "sp": 0.0, "rev": 0.0,
                                 "category": "", "last": "", "b_lt1": 0,
                                 "b_1_15": 0, "b_15_2": 0, "b_2_3": 0, "b_3p": 0})
    for a in ads.values():
        if a["product"] in SKIP_PRODUCTS or a["spend"] < min_spend_ad:
            continue
        k = (a["product"], a["ctype"], a["story"])
        cell = cells[k]
        cell["n"] += 1
        cell["wins"] += 1 if a["roas"] >= threshold else 0
        r = a["roas"]
        if r < 1:      cell["b_lt1"] += 1
        elif r < 1.5:  cell["b_1_15"] += 1
        elif r < 2:    cell["b_15_2"] += 1
        elif r < 3:    cell["b_2_3"] += 1
        else:          cell["b_3p"] += 1
        cell["sp"] += a["spend"]
        cell["rev"] += a["rev"]
        cell["category"] = a["category"]
        cell["last"] = max(cell["last"], a["last"])
    rows = []
    for (product, ct, st), c in cells.items():
        br = c["rev"] / c["sp"] if c["sp"] else 0.0
        rows.append({
            "product": product, "category": c["category"],
            "creative_type": ct, "storyline": st,
            "ads": c["n"],
            "roas_lt1": c["b_lt1"], "roas_1_15": c["b_1_15"],
            "roas_15_2": c["b_15_2"], "roas_2_3": c["b_2_3"],
            "roas_3plus": c["b_3p"], "wins": c["wins"],
            "win_rate": round(c["wins"] / c["n"], 2) if c["n"] else 0,
            "blended_roas": round(br, 2), "spend": round(c["sp"]),
            "last_active": c["last"],
        })
    rows.sort(key=lambda r: (r["product"], -r["spend"]))
    return rows


def do_not_push_rows(ads, verdict_rows, cutoff, threshold,
                     max_roas=1.0, min_spend=30000.0, min_ads=10,
                     min_camps=3, active_days=14, show_min_spend=5000.0):
    """Per (product, creative_type) ad-level status table — EVERY combo with real
    volume, each tagged:
      STOP  — outright money-loser with volume & recent: blended ROAS < max_roas,
              >= min_camps camps OR >= min_ads ads, >= min_spend sunk, still active.
              This is the 'never push again' subset.
      WEAK  — blended ROAS < 1.0 but below the STOP volume/spend bar (thin loser).
      WATCH — 1.0 <= blended ROAS < 1.5 (break-even-ish).
      KEEP  — 1.5 <= blended ROAS < threshold (profitable).
      SCALE — blended ROAS >= threshold (a clear winner — do more).
    Each row carries the product's best alternative type to redirect budget to.
    Rows below show_min_spend are dropped as noise. Sorted worst-first."""
    agg = defaultdict(lambda: {"ads": 0, "ge2": 0, "sp": 0.0, "rev": 0.0,
                               "category": "", "last": "", "cids": set()})
    for a in ads.values():
        if a["product"] in SKIP_PRODUCTS:
            continue
        k = (a["product"], a["ctype"])
        c = agg[k]
        c["ads"] += 1
        c["ge2"] += 1 if a["roas"] >= threshold else 0
        c["sp"] += a["spend"]
        c["rev"] += a["rev"]
        c["category"] = a["category"]
        c["last"] = max(c["last"], a["last"])
        if a.get("campaign_id"):
            c["cids"].add(str(a["campaign_id"]))
    camps = {(r["product"], r["creative_type"]): r["attempts"] for r in verdict_rows}
    best = defaultdict(lambda: ("", 0.0))
    for (p, t), c in agg.items():
        br = c["rev"] / c["sp"] if c["sp"] else 0.0
        if c["ads"] >= 2 and br > best[p][1]:
            best[p] = (t, br)
    try:
        recency = (date.fromisoformat(cutoff) - timedelta(days=active_days)).isoformat()
    except ValueError:
        recency = "0000-00-00"

    PRIORITY = {"STOP": 0, "WEAK": 1, "WATCH": 2, "KEEP": 3, "SCALE": 4}
    rows = []
    for (p, t), c in agg.items():
        if c["sp"] < show_min_spend:
            continue
        br = c["rev"] / c["sp"] if c["sp"] else 0.0
        nc = camps.get((p, t), 0)
        is_stop = (nc >= min_camps or c["ads"] >= min_ads) and br < max_roas \
            and c["sp"] >= min_spend and c["last"] >= recency
        if is_stop:
            status = "STOP"
        elif br >= threshold:
            status = "SCALE"
        elif br >= 1.5:
            status = "KEEP"
        elif br >= 1.0:
            status = "WATCH"
        else:
            status = "WEAK"
        bt, bbr = best[p]
        if status in ("SCALE", "KEEP"):
            use_instead, note = "— (this type works)", ""
        elif bt and bt != t and bbr >= 1.3:
            use_instead, note = f"{bt} ({bbr:.2f}×)", ""
        else:
            use_instead, note = "—", "no winning type on this product — review the product"
        rows.append({
            "product": p, "category": c["category"], "creative_type": t,
            "status": status, "ads": c["ads"], "ads_ge2": c["ge2"],
            "blended_roas": round(br, 2), "campaigns": len(c["cids"]) or nc,
            "spend": round(c["sp"]), "last_active": c["last"],
            "use_instead": use_instead, "note": note,
            "campaign_ids": ", ".join(sorted(c["cids"])),
            "verdict": status, "_pri": PRIORITY[status],
        })
    rows.sort(key=lambda r: (r["_pri"], -r["spend"]))  # worst (STOP) first
    return rows


# ── output ───────────────────────────────────────────────────────────────────
def write_csv(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"  wrote {path}  ({len(rows)} rows)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(_REPO / "state" / "ntn.db"))
    ap.add_argument("--threshold", type=float, default=2.0)
    ap.add_argument("--min-spend", type=float, default=3000.0,
                    help="min campaign spend to count as an attempt")
    ap.add_argument("--min-spend-ad", type=float, default=1000.0)
    ap.add_argument("--last-n", type=int, default=10)
    ap.add_argument("--days", type=int, default=None,
                    help="restrict to the last N days of data (windowed report)")
    ap.add_argument("--out", default=str(_REPO / "out"))
    ap.add_argument("--sheet-id", default=os.environ.get("CREATIVE_ELIM_SHEET_ID", ""))
    args = ap.parse_args()

    if not os.path.isfile(args.db):
        sys.exit(f"DB not found: {args.db}")
    print(f"Creative Elimination — db={args.db}  threshold={args.threshold}  "
          f"min_spend={args.min_spend:.0f}  last_n={args.last_n}"
          + (f"  WINDOW=last {args.days}d" if args.days else ""))
    rows, cutoff, start = load_rows(args.db, args.days)
    win = f"{start} → {cutoff}" if start else f"through {cutoff}"
    print(f"  loaded {len(rows)} ad-day rows · {win}")

    # NTN -> (product, category) map, used to recover 'Unmapped' campaigns
    _con = sqlite3.connect(args.db)
    ntn_map = {c: (p, cat) for c, p, cat in _con.execute(
        "SELECT ntn_code, product, category FROM product_ntn_labels")}
    _con.close()

    camps, ads = finalize(*aggregate(rows, ntn_map))

    # Noise control: slug-recovered products (category 'Unmapped*') are best-
    # effort name guesses and the long tail is typo'd/fragmented. Keep only the
    # ones with real money behind them; demote the rest back to 'Unmapped'.
    SLUG_FLOOR = 25000.0
    slug_spend = defaultdict(float)
    for c in camps.values():
        if c["category"] == "Unmapped*":
            slug_spend[c["product"]] += c["spend"]
    weak = {p for p, s in slug_spend.items() if s < SLUG_FLOOR}
    for d in (camps, ads):
        for c in d.values():
            if c["category"] == "Unmapped*" and c["product"] in weak:
                c["product"], c["category"] = "Unmapped", "Unmapped"
    by_product = product_campaigns(camps, args.min_spend)
    products = {c["product"] for c in camps.values()}
    print(f"  {len(products)} products · {len(by_product)} with >=1 real attempt")

    cross = build_sku_crosswalk(args.db, rows, products)

    v = verdicts(by_product, args.last_n, args.threshold)
    # attach SKU
    for r in v:
        code, method, _ = cross.get(r["product"], ("", "", ""))
        r["ntn_code"] = code
        r["sku_match"] = method
    l10 = last10_rows(by_product, args.last_n, args.threshold)
    for r in l10:
        r["ntn_code"] = cross.get(r["product"], ("", "", ""))[0]
    allcamps = all_campaign_rows(camps, args.threshold)
    for r in allcamps:
        r["ntn_code"] = cross.get(r["product"], ("", "", ""))[0]
    combo = combination_rows(ads, args.min_spend_ad, args.threshold)
    for r in combo:
        r["ntn_code"] = cross.get(r["product"], ("", "", ""))[0]
    # Short windows accrue less spend/volume, so relax the STOP bar so it still
    # surfaces clear money-losers without needing 7 weeks of data behind them.
    if args.days and args.days <= 14:
        stop_kw = dict(min_spend=12000.0, min_ads=6, min_camps=2,
                       active_days=args.days, show_min_spend=2500.0)
    else:
        stop_kw = {}
    stop = do_not_push_rows(ads, v, cutoff, args.threshold, **stop_kw)
    for r in stop:
        r["ntn_code"] = cross.get(r["product"], ("", "", ""))[0]
    gaps = [{"product": p, "ntn_code": c[0], "match_method": c[1],
             "ntn_label": c[2]} for p, c in sorted(cross.items())
            if p not in SKIP_PRODUCTS]

    out = Path(args.out)
    stamp = f"{cutoff}_last{args.days}d" if args.days else cutoff
    write_csv(out / f"creative_verdicts_{stamp}.csv", v,
              ["product", "ntn_code", "sku_match", "category", "creative_type",
               "top_storyline", "rank_in_product", "attempts", "wins", "win_rate",
               "blended_roas", "product_avg_roas", "spend", "all_attempts",
               "all_blended_roas", "verdict"])
    write_csv(out / f"creative_last10_{stamp}.csv", l10,
              ["product", "ntn_code", "category", "n", "started", "ended",
               "portal", "creative_type", "storyline", "spend", "roas",
               "result", "campaign"])
    write_csv(out / f"creative_combination_{stamp}.csv", combo,
              ["product", "ntn_code", "category", "creative_type", "storyline",
               "ads", "roas_lt1", "roas_1_15", "roas_15_2", "roas_2_3",
               "roas_3plus", "wins", "win_rate", "blended_roas", "spend",
               "last_active"])
    write_csv(out / f"creative_sku_gaps_{stamp}.csv", gaps,
              ["product", "ntn_code", "match_method", "ntn_label"])
    write_csv(out / f"creative_do_not_push_{stamp}.csv", stop,
              ["product", "ntn_code", "category", "creative_type", "status",
               "ads", "ads_ge2", "blended_roas", "campaigns", "spend",
               "last_active", "use_instead", "note", "campaign_ids"])
    write_csv(out / f"creative_all_campaigns_{stamp}.csv", allcamps,
              ["product", "ntn_code", "category", "campaign_id", "campaign",
               "creative_type", "storyline", "portal", "started", "last_active",
               "spend", "revenue", "roas", "result"])

    # quick summary
    from collections import Counter as _C
    vc = _C(r["verdict"] for r in v)
    unmatched = sum(1 for g in gaps if g["match_method"] == "UNMATCHED")
    print(f"\n  verdict rows: {len(v)}  (" +
          " · ".join(f"{k} {vc[k]}" for k in ("SCALE", "KEEP", "WATCH", "CUT", "TEST") if vc.get(k)) + ")")
    n_stop = sum(1 for r in stop if r["status"] == "STOP")
    stop_spend = sum(r["spend"] for r in stop if r["status"] == "STOP")
    print(f"  STATUS table: {len(stop)} product×type rows · "
          f"STOP={n_stop} (₹{stop_spend:,.0f} on money-losers)")
    print(f"  ALL CAMPAIGNS: {len(allcamps)} campaigns listed")
    print(f"  SKU: {len(gaps)-unmatched}/{len(gaps)} products matched to NTN "
          f"({unmatched} unmatched — see gaps csv)")

    if args.sheet_id:
        from creative_elimination_sheet import push  # local helper
        suffix = f" (last {args.days}d)" if args.days else ""
        window = f"{start} → {cutoff}" if start else f"through {cutoff}"
        push(args.sheet_id, cutoff, v, l10, combo, gaps, stop, allcamps,
             tab_suffix=suffix, window=window)


if __name__ == "__main__":
    main()
