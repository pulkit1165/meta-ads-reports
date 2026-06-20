#!/usr/bin/env python3
"""
Indian Crystal Brands — best-seller analysis → new tab in the Jewellery Trend
Radar sheet.

Question it answers for the operator: are CRYSTAL BRACELETS the best-selling
product for India's spiritual-crystal D2C brands? Tracks four:

  • Mesmerize       (mesmerizeindia.com)  — best-seller collection
  • Zen Crystals    (thezencrystals.com)  — best-seller collection
  • AstroTalk       (astrotalk.store)     — best-seller collection
  • World of Oorja  (worldofoorja.com)    — no best-seller collection exposed,
                                            so full-catalog composition is used

For each brand we pull the merchandised best-seller list (order = ranking),
classify every product by type (Bracelet / Mala / Pendant / Raw-Decor / …) and
report: % bracelets, whether the #1 + top-5 are bracelets, bracelet price band.

Writes ONE dated tab to the Trend Radar sheet (same SA / sheet as
jewellery_trend_radar.py). One-off / re-runnable; not on a cron.

Usage:
  GOOGLE_SERVICE_ACCOUNT_FILE=/abs/path/sa.json python3 scripts/v2/indian_crystal_brands.py
"""
import os
import re
import sys
from datetime import datetime, timezone

import requests
import gspread
from google.oauth2.service_account import Credentials

# Same sheet as the Jewellery Trend Radar (the "jewellery bot" sheet).
SHEET_ID = os.environ.get("JEWELLERY_RADAR_SHEET_ID",
                          "12ar4xGIsd4i1A6HZ7pr7_CG4ripp5JFypH4wUfBymnk")
UA = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
      "Accept": "application/json"}
TOP_SHOWN = 15

# brand → (base, best-seller collection handle | None for full-catalogue,
#          fallback handles, note). Studd Muffyn is the operator's OWN brand —
#          multi-category (skin/hair/crystals/nutra), so we compare its CRYSTAL
#          best-seller collection only (its overall #1 best-seller is skincare).
BRANDS = [
    ("Studd Muffyn (us)", "https://studd-muffyn.myshopify.com", "crystal-best-sellers",
        ["best-selling-crystals"], "OUR brand · crystal category only (overall best-seller = skincare)"),
    ("Mesmerize",      "https://mesmerizeindia.com", "natural-stone-best-seller", [], ""),
    ("Zen Crystals",   "https://thezencrystals.com", "best-sellers",             [], ""),
    ("AstroTalk",      "https://astrotalk.store",    "bestsellers",
        ["best-sellers", "bestseller-all", "ht-bestsellers-collection"], ""),
    ("World of Oorja", "https://worldofoorja.com",   None,
        ["frontpage", "all"], "no best-seller collection exposed → full catalogue"),
]


def fetch(base, handle=None, limit=60):
    url = f"{base}/collections/{handle}/products.json" if handle else f"{base}/products.json"
    try:
        r = requests.get(url, params={"limit": limit}, headers=UA, timeout=20)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    return (r.json() or {}).get("products") or []


def ptype(p):
    hay = " ".join([
        p.get("title", "") or "", p.get("product_type", "") or "",
        " ".join(p.get("tags", [])) if isinstance(p.get("tags"), list) else (p.get("tags") or ""),
    ]).lower()
    if re.search(r"bracelet|kada|kadha", hay):              return "Bracelet"
    if re.search(r"\bmala\b|japa|rosary", hay):             return "Mala"
    if re.search(r"pendant|locket|necklace|chain", hay):    return "Pendant/Neck"
    if re.search(r"anklet|payal", hay):                     return "Anklet"
    if re.search(r"\bring\b", hay):                         return "Ring"
    if re.search(r"\btree\b", hay):                         return "Tree"
    if re.search(r"murti|idol|statue|ganesh|buddha", hay):  return "Idol/Murti"
    if re.search(r"kit|combo|\bset\b|pack", hay):           return "Kit/Combo"
    if re.search(r"pyramid|tower|point|obelisk|geode|cluster|raw |tumble|rough|coin",
                 hay):                                      return "Raw/Decor"
    return "Other"


def min_price(p):
    pr = [float(v["price"]) for v in p.get("variants", []) if v.get("price")]
    return min(pr) if pr else 0.0


def analyse():
    out = []
    for name, base, handle, fallbacks, note in BRANDS:
        prods, used = fetch(base, handle), handle
        if not prods:
            for alt in fallbacks:
                prods = fetch(base, alt)
                if prods:
                    used = alt
                    break
        if not prods:
            prods = fetch(base)            # last resort: root catalogue
            used = used or "(full catalogue)"
        if not prods:
            print(f"  ✗ {name}: no data", file=sys.stderr)
            continue
        prods = [p for p in prods if "gift-card" not in (p.get("handle", "") or "")]
        n = len(prods)
        ranked = [{
            "rank": i + 1, "type": ptype(p), "price": min_price(p),
            "title": (p.get("title") or "").strip(),
            "url": f"{base}/products/{p.get('handle','')}",
        } for i, p in enumerate(prods)]
        brac = [r for r in ranked if r["type"] == "Bracelet"]
        bp = [r["price"] for r in brac if r["price"]]
        top5_brac = sum(1 for r in ranked[:5] if r["type"] == "Bracelet")
        types = {}
        for r in ranked:
            types[r["type"]] = types.get(r["type"], 0) + 1
        is_bs = used not in (None, "(full catalogue)", "frontpage", "all")
        out.append({
            "name": name, "base": base, "collection": used, "is_bestseller_coll": is_bs,
            "note": note,
            "n": n, "n_brac": len(brac), "pct": round(len(brac) * 100 / n) if n else 0,
            "rank1": ranked[0]["type"] if ranked else "—",
            "top5_brac": top5_brac,
            "bp_lo": int(min(bp)) if bp else 0, "bp_hi": int(max(bp)) if bp else 0,
            "types": types, "ranked": ranked[:TOP_SHOWN],
        })
        print(f"  {name}: {len(brac)}/{n} bracelets ({out[-1]['pct']}%), "
              f"#1={out[-1]['rank1']}, top5_brac={top5_brac}")
    return out


def verdict(b):
    rank1_brac = b["rank1"] == "Bracelet"
    if b["pct"] >= 60 and rank1_brac:
        return "✅ YES — bracelet is the hero"
    if rank1_brac and (b["pct"] >= 40 or b["top5_brac"] >= 3):
        return "✅ YES — bracelet leads"
    if rank1_brac:
        return "🟡 PARTIAL — #1 is a bracelet, mix is broader"
    if b["pct"] >= 40:
        return "🟡 PARTIAL — bracelet-heavy, but not #1"
    return "❌ NO — bracelet is not the lead"


def _link(url, text):
    return '=HYPERLINK("%s","%s")' % (url, (text or "").replace('"', '""')[:60])


def write_tab(data):
    sa = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
    if not sa or not os.path.isfile(sa):
        sys.exit(f"GOOGLE_SERVICE_ACCOUNT_FILE missing/invalid: {sa}")
    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(sa, scopes=scopes)
    sh = gspread.authorize(creds).open_by_key(SHEET_ID)

    today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    tab = f"IN Crystals {today}"
    titles = {w.title: w for w in sh.worksheets()}
    if tab in titles:
        ws = titles[tab]
    else:
        ws = sh.add_worksheet(title=tab, rows=200, cols=8)

    stamp = datetime.now(timezone.utc).astimezone().strftime("%d %b %y, %H:%M")
    n_yes = sum(1 for b in data if verdict(b).startswith("✅"))
    V, section_rows, hdr_rows = [], [], []

    V.append(["💠 Indian Crystal Brands — is the CRYSTAL BRACELET their best-seller?"])
    V.append([f"Shopify best-seller scrape · {len(data)} brands · built {stamp}. "
              f"Verdict: bracelet is the hero best-seller for {n_yes}/{len(data)} brands."])
    V.append([])

    # ── summary table ──
    section_rows.append(len(V)); V.append(["SUMMARY — crystal bracelet dominance"])
    hdr_rows.append(len(V))
    V.append(["Brand", "Best-seller list", "#Prod", "Bracelets",
              "% bracelet", "#1 type", "Top-5 brac", "Verdict"])
    for b in data:
        src = b["collection"] if b["is_bestseller_coll"] else f"{b['collection']} (no BS coll)"
        V.append([b["name"], src, b["n"], b["n_brac"], f"{b['pct']}%",
                  b["rank1"], f"{b['top5_brac']}/5", verdict(b)])
    V.append([])

    # ── per-brand top sellers ──
    for b in data:
        bandtxt = (f" · bracelet price ₹{b['bp_lo']}–{b['bp_hi']}" if b["bp_hi"] else "")
        mix = ", ".join(f"{k} {v}" for k, v in sorted(b["types"].items(), key=lambda x: -x[1]))
        section_rows.append(len(V))
        V.append([f"🏆 {b['name']} — top {len(b['ranked'])} best-sellers"
                  f"  ({b['pct']}% bracelets{bandtxt})"])
        V.append([f"type mix: {mix}" + (f"   ⚠️ {b['note']}" if b.get("note") else "")])
        hdr_rows.append(len(V))
        V.append(["#", "Type", "Price ₹", "Product", "", "", "", ""])
        for r in b["ranked"]:
            V.append([r["rank"], r["type"], int(r["price"]),
                      _link(r["url"], r["title"]), "", "", "", ""])
        V.append([])

    ws.clear()
    ws.update(range_name="A1", values=V, value_input_option="USER_ENTERED")

    # ── formatting ──
    sid = ws.id
    dark = {"red": 0.16, "green": 0.10, "blue": 0.24}
    grey = {"red": 0.90, "green": 0.90, "blue": 0.92}
    lgrey = {"red": 0.96, "green": 0.96, "blue": 0.97}
    white = {"red": 1, "green": 1, "blue": 1}

    def fmt(r0, r1, c0, c1, body, fields):
        return {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": r0, "endRowIndex": r1,
                "startColumnIndex": c0, "endColumnIndex": c1},
                "cell": {"userEnteredFormat": body}, "fields": fields}}

    reqs = [fmt(0, 1, 0, 8, {"backgroundColor": dark,
            "textFormat": {"bold": True, "fontSize": 13, "foregroundColor": white}},
            "userEnteredFormat(backgroundColor,textFormat)")]
    for r in section_rows:
        reqs.append(fmt(r, r + 1, 0, 8, {"backgroundColor": grey,
                    "textFormat": {"bold": True, "fontSize": 11}},
                    "userEnteredFormat(backgroundColor,textFormat)"))
    for r in hdr_rows:
        reqs.append(fmt(r, r + 1, 0, 8, {"backgroundColor": lgrey, "textFormat": {"bold": True}},
                    "userEnteredFormat(backgroundColor,textFormat)"))
    reqs.append({"updateSheetProperties": {
        "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 2}},
        "fields": "gridProperties.frozenRowCount"}})
    reqs.append({"autoResizeDimensions": {"dimensions": {"sheetId": sid,
                "dimension": "COLUMNS", "startIndex": 0, "endIndex": 8}}})
    sh.batch_update({"requests": reqs})
    return tab


def main():
    print("Indian Crystal Brands — best-seller bracelet analysis")
    data = analyse()
    if not data:
        sys.exit("No brand data fetched.")
    tab = write_tab(data)
    print(f"\n  Wrote tab '{tab}' to sheet {SHEET_ID}")


if __name__ == "__main__":
    main()
