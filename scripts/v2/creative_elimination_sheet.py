#!/usr/bin/env python3
"""Google Sheet writer for creative_elimination.py.

push() writes four tabs to an operator-owned sheet that the service account
antriksh-bot@ already has Editor on (the SA itself has zero Drive quota, so it
cannot create sheets — the operator creates a blank sheet, shares it, and passes
the id). Tabs:
  KEEP-CUT          verdict per product x creative-type (colour-coded)
  Last 10 Campaigns per product, its last N campaign attempts
  Combinations      product x (creative-type x storyline) ad-level rollup
  SKU Gaps          product -> NTN crosswalk, UNMATCHED flagged
"""
import os
import sys

import gspread
from google.oauth2.service_account import Credentials

VERDICT_COLOR = {
    "SCALE": {"red": 0.71, "green": 0.88, "blue": 0.70},   # strong green
    "KEEP":  {"red": 0.85, "green": 0.94, "blue": 0.83},   # light green
    "WATCH": {"red": 1.00, "green": 0.95, "blue": 0.70},   # yellow
    "CUT":   {"red": 0.96, "green": 0.78, "blue": 0.76},   # red
    "TEST":  {"red": 0.92, "green": 0.92, "blue": 0.92},   # grey
    "STOP":  {"red": 0.90, "green": 0.49, "blue": 0.45},   # hard red
    "WEAK":  {"red": 0.96, "green": 0.78, "blue": 0.76},   # red
}


def _open(sheet_id):
    sa = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
    if not sa or not os.path.isfile(sa):
        sys.exit(f"GOOGLE_SERVICE_ACCOUNT_FILE missing/invalid: {sa}")
    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(sa, scopes=scopes)
    return gspread.authorize(creds).open_by_key(sheet_id)


def _tab(sh, title, rows_hint=400, cols_hint=20):
    titles = {w.title: w for w in sh.worksheets()}
    if title in titles:
        return titles[title]
    if "Sheet1" in titles:
        ws = titles["Sheet1"]
        ws.update_title(title)
        return ws
    return sh.add_worksheet(title=title, rows=rows_hint, cols=cols_hint)


def _fmt(sid, r0, r1, c0, c1, body, fields):
    return {"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": r0, "endRowIndex": r1,
                  "startColumnIndex": c0, "endColumnIndex": c1},
        "cell": {"userEnteredFormat": body}, "fields": fields}}


def _write(ws, header_title, subtitle, columns, colkeys, rows,
           verdict_col=None, freeze=3):
    grey = {"red": 0.90, "green": 0.90, "blue": 0.90}
    dark = {"red": 0.12, "green": 0.12, "blue": 0.20}
    white = {"red": 1, "green": 1, "blue": 1}
    V = [[header_title], [subtitle], columns]
    for r in rows:
        V.append([r.get(c, "") for c in colkeys])
    ncol = len(columns)
    ws.clear()
    ws.resize(rows=max(len(V) + 10, 20), cols=max(ncol, 5))  # fit the grid
    ws.update(range_name="A1", values=V, value_input_option="RAW")
    sid = ws.id
    reqs = [
        _fmt(sid, 0, 1, 0, ncol,
             {"backgroundColor": dark,
              "textFormat": {"bold": True, "fontSize": 13, "foregroundColor": white}},
             "userEnteredFormat(backgroundColor,textFormat)"),
        _fmt(sid, 2, 3, 0, ncol,
             {"backgroundColor": grey, "textFormat": {"bold": True}},
             "userEnteredFormat(backgroundColor,textFormat)"),
        {"updateSheetProperties": {
            "properties": {"sheetId": sid,
                           "gridProperties": {"frozenRowCount": freeze}},
            "fields": "gridProperties.frozenRowCount"}},
        {"autoResizeDimensions": {
            "dimensions": {"sheetId": sid, "dimension": "COLUMNS",
                           "startIndex": 0, "endIndex": ncol}}},
    ]
    if verdict_col is not None:
        for i, r in enumerate(rows):
            col = VERDICT_COLOR.get(r.get("verdict"))
            if col:
                rr = 3 + i  # data starts at row index 3 (0-based)
                reqs.append(_fmt(sid, rr, rr + 1, verdict_col, verdict_col + 1,
                                 {"backgroundColor": col,
                                  "textFormat": {"bold": True}},
                                 "userEnteredFormat(backgroundColor,textFormat)"))
    ws.spreadsheet.batch_update({"requests": reqs})


# column key order per tab (maps dict rows -> sheet columns)
COLKEYS = {
    "DO NOT PUSH": ["product", "ntn_code", "category", "creative_type", "status",
                    "ads", "ads_ge2", "blended_roas", "campaigns", "spend",
                    "last_active", "use_instead", "note", "campaign_ids"],
    "KEEP-CUT": ["product", "ntn_code", "category", "creative_type",
                 "top_storyline", "rank_in_product", "attempts", "wins",
                 "win_rate", "blended_roas", "product_avg_roas", "spend",
                 "all_attempts", "all_blended_roas", "verdict"],
    "Last 10 Campaigns": ["product", "ntn_code", "category", "n", "started",
                          "ended", "portal", "creative_type", "storyline",
                          "spend", "roas", "result", "campaign"],
    "Combinations": ["product", "ntn_code", "category", "creative_type",
                     "storyline", "ads", "roas_lt1", "roas_1_15", "roas_15_2",
                     "roas_2_3", "roas_3plus", "wins", "win_rate",
                     "blended_roas", "spend", "last_active"],
    "SKU Gaps": ["product", "ntn_code", "match_method", "ntn_label"],
    "All Campaigns": ["product", "ntn_code", "category", "campaign_id",
                      "campaign", "creative_type", "storyline", "portal",
                      "started", "last_active", "spend", "revenue", "roas",
                      "result"],
    "Campaign ROI": ["campaign_id", "product", "creative_type", "roas"],
}

HEADERS = {
    "DO NOT PUSH": ["Product", "SKU", "Category", "Creative Type", "Status",
                    "Ads", "Ads≥2", "Blended ROAS", "Camps", "Spent ₹",
                    "Last Active", "✅ Use Instead", "Note", "Campaign IDs"],
    "KEEP-CUT": ["Product", "SKU", "Category", "Creative Type", "Top Storyline",
                 "Rank", "Attempts(L10)", "Wins≥2", "Win%", "Blended ROAS",
                 "Prod Avg", "Spend ₹", "All Att.", "All ROAS", "Verdict"],
    "Last 10 Campaigns": ["Product", "SKU", "Category", "#", "Started", "Ended",
                          "Portal", "Creative Type", "Storyline", "Spend ₹",
                          "ROAS", "Result", "Campaign"],
    "Combinations": ["Product", "SKU", "Category", "Creative Type", "Storyline",
                     "Ads", "ROAS<1", "ROAS 1–1.5", "ROAS 1.5–2", "ROAS 2–3",
                     "ROAS 3+", "Wins≥2", "Win%", "Blended ROAS", "Spend ₹",
                     "Last Active"],
    "SKU Gaps": ["Product", "NTN SKU", "Match method", "NTN label"],
    "All Campaigns": ["Product", "SKU", "Category", "Campaign ID", "Campaign",
                      "Creative Type", "Storyline", "Portal", "Started",
                      "Last Active", "Spend ₹", "Revenue ₹", "ROAS", "Result"],
    "Campaign ROI": ["Campaign ID", "Product", "Creative Type", "ROI"],
}


def push(sheet_id, cutoff, verdicts, last10, combos, gaps, stop=None,
         allcamps=None, threshold=2.0, tab_suffix="", window=None):
    """tab_suffix lets a windowed run (e.g. ' (last 10d)') write a parallel set
    of tabs without clobbering the full-history report. window is a human label
    like '2026-06-10 → 2026-06-19' shown in each tab's subtitle."""
    sh = _open(sheet_id)
    span = window or f"through {cutoff}"
    n_scale = sum(1 for r in verdicts if r["verdict"] == "SCALE")
    n_cut = sum(1 for r in verdicts if r["verdict"] == "CUT")
    sub = (f"Data {span}  ·  WIN = blended ROAS ≥ {threshold:g}  ·  "
           f"verdict is relative to each product's own avg  ·  "
           f"{n_scale} SCALE, {n_cut} CUT signals")
    windowed = bool(tab_suffix)

    def T(name):
        return _tab(sh, name + tab_suffix)

    # DO NOT PUSH — full status table for EVERY product×type (STOP at top, red).
    if stop is not None:
        n_stop = sum(1 for r in stop if r.get("status") == "STOP")
        waste = sum(r["spend"] for r in stop if r.get("status") == "STOP")
        _write(T("DO NOT PUSH"),
               "⛔ Creative status by product — STOP / WEAK / WATCH / KEEP / SCALE",
               f"Data {span}  ·  every product × creative-type, worst first  ·  "
               f"STOP (red) = money-loser, no more attempts  ·  "
               f"{n_stop} STOP rows, ₹{waste:,.0f} sunk  ·  SCALE/KEEP = do more",
               HEADERS["DO NOT PUSH"], COLKEYS["DO NOT PUSH"], stop, verdict_col=4)

    # KEEP-CUT — sort by category, product, rank
    v = sorted(verdicts, key=lambda r: (r["category"], r["product"],
                                        r["rank_in_product"]))
    _write(T("KEEP-CUT"), "\U0001f9ed Creative KEEP / CUT by product",
           sub, HEADERS["KEEP-CUT"], COLKEYS["KEEP-CUT"], v, verdict_col=14)

    cap = "all campaigns in window" if windowed else "Last 10 campaign attempts"
    l10 = sorted(last10, key=lambda r: (r["product"], r["n"]))
    _write(T("Last 10 Campaigns"),
           f"\U0001f553 {cap} per product",
           f"Data {span}  ·  newest first  ·  WIN = ROAS ≥ {threshold:g}",
           HEADERS["Last 10 Campaigns"], COLKEYS["Last 10 Campaigns"], l10)

    _write(T("Combinations"),
           "\U0001f9ea Creative-type × storyline (ad-level)",
           f"Data {span}  ·  ad creatives with spend ≥ ₹1k",
           HEADERS["Combinations"], COLKEYS["Combinations"], combos)

    _write(T("SKU Gaps"),
           "\U0001f517 Product → NTN SKU crosswalk",
           "UNMATCHED = fill the NTN code manually (multi-SKU buckets stay blank)",
           HEADERS["SKU Gaps"], COLKEYS["SKU Gaps"], gaps, freeze=3)

    n_tabs = 5
    if allcamps is not None:
        _write(T("All Campaigns"),
               "\U0001f4cb All campaigns — every product, creative type & ROAS",
               f"Data {span}  ·  one row per campaign (spend ≥ ₹500)  ·  "
               f"{len(allcamps)} campaigns  ·  WIN = ROAS ≥ {threshold:g}  ·  "
               f"newest first within each product",
               HEADERS["All Campaigns"], COLKEYS["All Campaigns"], allcamps)
        # slim view: Campaign ID · Product · Creative Type · ROI
        _write(T("Campaign ROI"),
               "\U0001f4b0 Campaign → Product → Creative Type → ROI",
               f"Data {span}  ·  one row per campaign  ·  ROI = revenue ÷ spend",
               HEADERS["Campaign ROI"], COLKEYS["Campaign ROI"], allcamps)
        n_tabs = 7

    print(f"  pushed {n_tabs} tabs ('{tab_suffix.strip() or 'full'}') to "
          f"https://docs.google.com/spreadsheets/d/{sheet_id}")
