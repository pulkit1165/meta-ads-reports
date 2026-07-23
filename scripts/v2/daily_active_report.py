#!/usr/bin/env python3
"""
Meta Ads — Daily Active Campaign Report.

Every ACTIVE campaign across all Meta accounts (SM / SML / NBP), with today's
spend + ROAS and a prospecting-vs-retargeting classification derived from the
ad sets' actual audience targeting (not the campaign name).

Columns (one dated tab in the CATEGORY WISE BUDGET sheet):
  Ad Account | Campaign Name | Campaign ID | Status | Daily Budget |
  Spend Today | Spend % | ROAS | Audience Type |
  Retargeting Inclusions | Prospecting Exclusions

Classification:
  • Retargeting  → an active ad set includes a NON-lookalike custom audience
                   (website visitors / ATC / IC / purchasers / engagement /
                   video viewers / CRM). Inclusions listed; exclusions = N/A.
  • Prospecting  → broad / interest / lookalike / open. Exclusions listed
                   (or "No exclusions found"); inclusions = N/A.

Budget = effective ₹/day (daily as-is; lifetime ÷ schedule days; CBO-off =
sum of active ad-set budgets). Spend/ROAS = TODAY (partial day) from the live
Meta Insights API. revenue/ROAS are Meta-pixel (over-state vs Shopify).

Needs META_ACCESS_TOKEN + GOOGLE_SERVICE_ACCOUNT_FILE.
Usage: python3 scripts/v2/daily_active_report.py
"""
import os
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import requests
import gspread
from google.oauth2.service_account import Credentials

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _utils import IST, PORTAL_ACCOUNTS  # noqa: E402
from category_budget_sheet import per_day_budget  # noqa: E402

GRAPH = "https://graph.facebook.com/v21.0"
SHEET_ID = "1aZXYLPPqi7LgukH6ixmmy5WnbFz8xLKcwZpdXYwthGY"  # CATEGORY WISE BUDGET
TODAY = datetime.now(IST).date().isoformat()

# Accounts to skip — campaigns are "active" but their ad sets are paused, so
# they neither deliver nor have audience targeting to report (operator's call).
SKIP_ACCOUNTS = {"SM Hair", "SM CL 06"}

OBJ_MAP = {
    "OUTCOME_SALES": "Sales", "PRODUCT_CATALOG_SALES": "Sales", "CONVERSIONS": "Sales",
    "OUTCOME_LEADS": "Leads", "LEAD_GENERATION": "Leads",
    "OUTCOME_TRAFFIC": "Traffic", "LINK_CLICKS": "Traffic",
    "OUTCOME_AWARENESS": "Awareness", "REACH": "Awareness", "VIDEO_VIEWS": "Awareness",
    "OUTCOME_ENGAGEMENT": "Engagement", "POST_ENGAGEMENT": "Engagement",
    "OUTCOME_APP_PROMOTION": "App",
}
S = requests.Session()


# Meta transient / rate-limit error codes worth retrying (not permission #200).
_RETRY_CODES = {1, 2, 4, 17, 32, 341, 368, 613, 80000, 80001, 80002, 80003, 80004}


def get(url, params):
    for i in range(6):
        try:
            r = S.get(url, params=params, timeout=120)
            j = r.json()
        except requests.RequestException:
            time.sleep(3 * (i + 1))
            continue
        err = j.get("error") if isinstance(j, dict) else None
        if err and (err.get("code") in _RETRY_CODES or r.status_code in (500, 503)):
            time.sleep(5 * (i + 1))   # transient → back off and retry
            continue
        return j
    return {"error": {"message": "request failed after retries"}}


def fetch_all(url, params):
    """Paginate a Graph edge → list of data rows. None on hard error."""
    out = []
    while True:
        j = get(url, params)
        if "error" in j:
            return out if out else None
        out += j.get("data", [])
        nxt = j.get("paging", {}).get("next")
        if not nxt:
            return out
        url, params = nxt, None


def is_lookalike(name):
    n = (name or "").lower()
    return any(k in n for k in ("lookalike", "look alike", " lla", "lla ", "_lla", " lal", "lal_", "-lal"))


def insights_today(aid, token):
    out = {}
    rows = fetch_all(f"{GRAPH}/{aid}/insights",
                     {"level": "campaign", "date_preset": "today",
                      "fields": "campaign_id,spend,action_values", "limit": 200,
                      "access_token": token})
    for r in rows or []:
        rev = next((float(a["value"]) for a in (r.get("action_values") or [])
                    if a.get("action_type") == "omni_purchase"), 0.0)
        out[r["campaign_id"]] = (float(r.get("spend") or 0), rev)
    return out


def adset_audiences(aid, token):
    """{campaign_id: {'inc':[names], 'exc':[names], 'has_active':True}} from
    ACTIVE ad sets only. Returns None if the ad-set fetch hard-fails (so the
    caller keeps the campaigns instead of dropping them as 'no active ad set')."""
    rows = fetch_all(f"{GRAPH}/{aid}/adsets",
                     {"fields": "campaign_id,effective_status,"
                                "targeting{custom_audiences,excluded_custom_audiences}",
                      "effective_status": '["ACTIVE"]', "limit": 200,
                      "access_token": token})
    if rows is None:
        return None  # fetch failed (e.g. rate-limited) — don't treat as "all off"
    agg = defaultdict(lambda: {"inc": {}, "exc": {}, "has_active": False})
    for a in rows or []:
        cid = a.get("campaign_id")
        if not cid:
            continue
        agg[cid]["has_active"] = True
        t = a.get("targeting", {}) or {}
        for x in (t.get("custom_audiences") or []):
            agg[cid]["inc"][x.get("id")] = x.get("name") or x.get("id")
        for x in (t.get("excluded_custom_audiences") or []):
            agg[cid]["exc"][x.get("id")] = x.get("name") or x.get("id")
    return {k: {"inc": list(v["inc"].values()), "exc": list(v["exc"].values()),
                "has_active": v["has_active"]} for k, v in agg.items()}


def classify(aud):
    """Return (audience_type, inclusions_cell, exclusions_cell)."""
    if aud is None or not aud.get("has_active"):
        return "N/A", "N/A", "N/A"
    inc = aud.get("inc", [])
    exc = aud.get("exc", [])
    real_incl = [n for n in inc if not is_lookalike(n)]
    if real_incl:
        return "Retargeting", "; ".join(real_incl), "N/A"
    # prospecting (broad / interest / lookalike / open)
    exc_cell = "; ".join(exc) if exc else "No exclusions found"
    return "Prospecting", "N/A", exc_cell


def collect():
    token = os.environ.get("META_ACCESS_TOKEN")
    if not token:
        sys.exit("META_ACCESS_TOKEN not set")
    rows = []
    blocked = []
    for portal, accts in PORTAL_ACCOUNTS.items():
        for env, friendly in accts:
            aid = os.environ.get(env)
            if not aid:
                continue
            if friendly in SKIP_ACCOUNTS:
                print(f"  — {portal} · {friendly}: skipped (ad sets off)")
                continue
            label = f"{portal} · {friendly}"
            camps = fetch_all(f"{GRAPH}/{aid}/campaigns",
                              {"fields": "id,name,effective_status,objective,daily_budget,"
                                         "lifetime_budget,start_time,stop_time,"
                                         "adsets.limit(50){effective_status,daily_budget,lifetime_budget}",
                               "effective_status": '["ACTIVE"]', "limit": 400,
                               "access_token": token})
            if camps is None:
                blocked.append(label)
                print(f"  ✗ {label}: blocked / error", file=sys.stderr)
                continue
            ins = insights_today(aid, token)
            auds = adset_audiences(aid, token)
            n = skipped_off = 0
            for c in camps:
                aud = auds.get(c["id"]) if auds is not None else None
                # If we successfully fetched ad sets and this campaign has none
                # active, it isn't really delivering — drop it (ad sets off).
                if auds is not None and aud is None:
                    skipped_off += 1
                    continue
                bud = per_day_budget(c)
                sp, rev = ins.get(c["id"], (0.0, 0.0))
                atype, incl, excl = classify(aud)
                rows.append({
                    "acct": label, "name": c.get("name", ""), "id": c["id"],
                    "status": c.get("effective_status", ""),
                    "obj": OBJ_MAP.get(c.get("objective", ""), c.get("objective", "") or "N/A"),
                    "bud": bud, "sp": sp, "rev": rev,
                    "pct": (sp / bud * 100 if bud else 0),
                    "roas": (rev / sp if sp else 0),
                    "atype": atype, "incl": incl, "excl": excl,
                })
                n += 1
            note = f" ({skipped_off} dropped: ad sets off)" if skipped_off else ""
            if auds is None:
                note += " ⚠️ audience N/A (rate-limited)"
            print(f"  {label}: {n} delivering{note}")
            time.sleep(0.5)
    return rows, blocked


# ── sheet ─────────────────────────────────────────────────────────────────────
def write_sheet(rows, blocked):
    sa = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
    if not sa or not os.path.isfile(sa):
        sys.exit(f"GOOGLE_SERVICE_ACCOUNT_FILE missing/invalid: {sa}")
    creds = Credentials.from_service_account_file(
        sa, scopes=["https://www.googleapis.com/auth/spreadsheets",
                    "https://www.googleapis.com/auth/drive"])
    sh = gspread.authorize(creds).open_by_key(SHEET_ID)
    tab = f"ActiveReport {TODAY}"
    titles = {w.title: w for w in sh.worksheets()}
    ws = titles[tab] if tab in titles else sh.add_worksheet(title=tab, rows=max(50, len(rows) + 60), cols=11)

    tot_sp = sum(r["sp"] for r in rows)
    tot_bud = sum(r["bud"] for r in rows)
    tot_rev = sum(r["rev"] for r in rows)
    stamp = datetime.now(IST).strftime("%d %b %y, %H:%M IST")

    V = []
    sect, hdr, tot_rowi = [], [], []
    V.append([f"📋 Meta Ads — Daily Active Campaign Report · {TODAY}"])
    V.append([f"All ACTIVE campaigns, live from Meta. Spend/ROAS = TODAY (partial day), "
              f"Meta-pixel. Refreshed {stamp}."
              + (f"  ⚠️ blocked (no API access): {', '.join(blocked)}" if blocked else "")])
    V.append([])
    hdr.append(len(V))
    V.append(["SUMMARY", "Active campaigns", "Spend today", "Allocated budget",
              "Overall spend %", "Overall ROAS", "", "", "", "", ""])
    V.append(["", len(rows), round(tot_sp), round(tot_bud),
              f"{(tot_sp/tot_bud*100 if tot_bud else 0):.1f}%",
              f"{(tot_rev/tot_sp if tot_sp else 0):.2f}x", "", "", "", "", ""])
    V.append([])

    COLS = ["Ad Account", "Campaign Name", "Campaign ID", "Status", "Daily Budget",
            "Spend Today", "Spend %", "ROAS", "Audience Type",
            "Retargeting Inclusions", "Prospecting Exclusions"]

    by_acct = defaultdict(list)
    for r in rows:
        by_acct[r["acct"]].append(r)
    for acct in sorted(by_acct, key=lambda a: -sum(r["sp"] for r in by_acct[a])):
        sub = sorted(by_acct[acct], key=lambda r: -r["sp"])
        a_sp = sum(r["sp"] for r in sub)
        a_bud = sum(r["bud"] for r in sub)
        sect.append(len(V))
        V.append([f"{acct}  —  {len(sub)} active · ₹{a_sp:,.0f} spent · ₹{a_bud:,.0f} budget"])
        hdr.append(len(V))
        V.append(COLS)
        for r in sub:
            V.append([r["acct"], r["name"], r["id"], r["status"], round(r["bud"]),
                      round(r["sp"]), f"{r['pct']:.0f}%", f"{r['roas']:.2f}x",
                      r["atype"], r["incl"], r["excl"]])
        V.append([])

    ws.resize(rows=len(V) + 10, cols=11)
    ws.clear()
    ws.update(range_name="A1", values=V, value_input_option="USER_ENTERED")

    sid = ws.id
    blue = {"red": 0.10, "green": 0.46, "blue": 0.95}
    sec = {"red": 0.85, "green": 0.90, "blue": 1.0}
    hd = {"red": 0.93, "green": 0.95, "blue": 1.0}
    white = {"red": 1, "green": 1, "blue": 1}

    def fill(r, color, fg=None, size=None):
        tf = {"bold": True}
        if fg:
            tf["foregroundColor"] = fg
        if size:
            tf["fontSize"] = size
        return {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": r, "endRowIndex": r + 1,
                "startColumnIndex": 0, "endColumnIndex": 11},
                "cell": {"userEnteredFormat": {"backgroundColor": color, "textFormat": tf}},
                "fields": "userEnteredFormat(backgroundColor,textFormat)"}}

    reqs = [fill(0, blue, fg=white, size=13)]
    for r in hdr:
        reqs.append(fill(r, hd))
    for r in sect:
        reqs.append(fill(r, sec))
    reqs.append({"updateSheetProperties": {
        "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 5}},
        "fields": "gridProperties.frozenRowCount"}})
    reqs.append({"autoResizeDimensions": {"dimensions": {"sheetId": sid,
                "dimension": "COLUMNS", "startIndex": 0, "endIndex": 11}}})
    sh.batch_update({"requests": reqs})
    return tab, tot_sp, tot_bud, tot_rev


def main():
    print(f"Meta Daily Active Report — {datetime.now(IST):%d %b %y %H:%M IST}")
    rows, blocked = collect()
    if not rows:
        sys.exit("No active campaigns collected.")
    tab, tot_sp, tot_bud, tot_rev = write_sheet(rows, blocked)
    print(f"\n  {len(rows)} active campaigns | ₹{tot_sp:,.0f} spent today | "
          f"₹{tot_bud:,.0f} budget | overall ROAS {(tot_rev/tot_sp if tot_sp else 0):.2f}x")
    # per-account + audience-type rollup for the chat summary
    by = defaultdict(lambda: [0, 0.0, 0.0, 0.0])
    for r in rows:
        a = by[r["acct"]]
        a[0] += 1; a[1] += r["sp"]; a[2] += r["bud"]; a[3] += r["rev"]
    print("\n  ── by account (spend desc) ──")
    for acct, v in sorted(by.items(), key=lambda kv: -kv[1][1]):
        roas = v[3] / v[1] if v[1] else 0
        print(f"   {acct:<26} {v[0]:>4} camps | ₹{v[1]:>9,.0f} spent | ₹{v[2]:>9,.0f} bud | {roas:.2f}x")
    at = defaultdict(lambda: [0, 0.0])
    for r in rows:
        at[r["atype"]][0] += 1; at[r["atype"]][1] += r["sp"]
    print("\n  ── by audience type ──")
    for t, v in sorted(at.items(), key=lambda kv: -kv[1][1]):
        print(f"   {t:<14} {v[0]:>4} camps | ₹{v[1]:,.0f} spent today")
    if blocked:
        print(f"\n  ⚠️ blocked (no API access → N/A): {', '.join(blocked)}")
    print(f"\n  Tab '{tab}' written to sheet {SHEET_ID}")


if __name__ == "__main__":
    main()
