# NTN Meta Ads Reporting System — Master Documentation
**Last Updated:** 24 Apr 2026  
**Author:** Antriksh (AI Operator for Pulkit Sharma)  
**Purpose:** Complete reference for all automated Meta Ads reports. Feed this to any AI to understand, recreate, or extend the system.

---

## 🏗️ Infrastructure Overview

### Portals
Three Shopify stores, each with their own Meta ad accounts:
| Portal | Store | Tab Prefix |
|--------|-------|------------|
| SM | studd-muffyn.myshopify.com | SM |
| SML | studdmuffynlife.myshopify.com | SML |
| NBP | 472d21.myshopify.com | NBP |

### Meta Ad Accounts
| Key | Account ID | Portal |
|-----|-----------|--------|
| SM_FRAGRANCE_01 | act_466922745634023 | SM |
| SM_SKIN | act_578075381064759 | SM |
| SM_HAIR | act_944634709928295 | SM |
| SM_CRYSTALS | act_1181596092752041 | SM |
| SM_PERFUME | act_935485377999005 | SM |
| SM_CREDIT_LINE_05 | act_1196805438052488 | SM |
| SM_CREDIT_LINE_06 | act_1710314299750453 | SM |
| SML_SKIN | act_918587349998103 | SML |
| SML_HAIR | act_1229831035065328 | SML |
| SML_CRYSTALS | act_578721778180301 | SML |
| SML_CL_06 | act_2227792690890157 | SML |
| SML_CL_07 | act_830305079053577 | SML |
| NBP_SKIN | act_1505319823511657 | NBP |
| NBP_HAIR_PERFUME | act_1501832634098072 | NBP |
| NBP_CRYSTALS | act_1106988370948991 | NBP |

### Key Files
| File | Purpose |
|------|---------|
| `campaign_tracker_builder.py` | Builds SM/SML/NBP tracker tabs |
| `campaign_tracker_reports.py` | Builds Category/Audience/Product reports |
| `closing_watchlist.py` | 4x daily closing watchlist tab |
| `closing_camps_report.py` | Live closing recommendations |
| `live_camp_monitor.py` | 3-hourly live monitor + WA alerts |
| `build_creative_dashboard.py` | EC2 creative dashboard data |
| `run_daily_reports.py` | Master pipeline (4:30 AM IST cron) |
| `product_catalogue.py` | Campaign name → Product + Category mapping |

### Google Sheets
| Sheet | ID | Purpose |
|-------|----|---------|
| Campaign Tracker | `11IAPsJlil75aehYf5IzpSaTCLcAgPk9-57p6ZuPNNQM` | All tracker tabs + reports |
| NTN Dashboard | `1squ0JkqwiyFwIMRmqWc3q_AWHQtihn5o4dbDGyv7sAY` | KPI daily, portal master |

### Automation Schedule
| Time (IST) | Script | What it does |
|------------|--------|-------------|
| 4:30 AM | `run_daily_reports.py` | Full pipeline: builder → reports → creative → closing |
| 5:00 AM | EC2 cron | Creative dashboard refresh |
| 10:00 AM | `closing_watchlist.py` | First closing watchlist (fresh tab) |
| 12:00 PM | `closing_watchlist.py` | Second run (appends, shows closed since 10AM) |
| 3:00 PM | `closing_watchlist.py` | Third run |
| 6:00 PM | `closing_watchlist.py` | Final run |
| Every 3 hrs | `live_camp_monitor.py` | Live ROAS monitor tab |
| 1:00 PM | `live_camp_monitor.py --closing --notify` | WA closing alert to Pulkit |

---

## 📋 Report 1: Campaign Tracker

### What it is
A daily tab (`SM DD MMM YY`, `SML DD MMM YY`, `NBP DD MMM YY`) with one row per active campaign that had spend on the target date. This is the central data source for all other reports.

### Script
`campaign_tracker_builder.py --all --date YYYY-MM-DD`

### When it runs
4:30 AM IST daily (via `run_daily_reports.py`) for yesterday's date.

### Logic
1. For each portal, loop all accounts
2. Pull `campaigns` with `effective_status: ACTIVE/PAUSED` (not deleted) and `fields: id, name, status, start_time, daily_budget, lifetime_budget, attribution_spec, adsets{daily_budget,lifetime_budget}`
3. Pull `insights` at campaign level for the target date, with 4 attribution windows:
   - **1D (H column):** `1d_click`, date range = target date only
   - **2D (I column):** `1d_click`, date range = last 2 days
   - **3D (J column):** `1d_click`, date range = last 3 days  
   - **7D (K column):** `7d_click`, date range = last 7 days
4. For each campaign with spend > 0, build one row
5. **CRITICAL: De-duplicate by campaign_id** — collect all accounts per portal into a dict keyed by CID, then write ONE tab per portal. This prevents duplicate rows.

### ROAS Extraction
```python
# From purchase_roas field in insights API
# Each entry has action_type, value (default), 1d_click, 7d_click keys
for item in purchase_roas:
    if item['action_type'] == 'omni_purchase':
        roas_1d = item.get('1d_click')   # H column
        roas_7d = item.get('7d_click')   # K column
```

### Budget Logic
```python
# CBO campaign: has campaign-level daily_budget
budget = campaign.daily_budget / 100  # Meta returns in paise

# ABO campaign: daily_budget = 0, budget is at adset level
# Sum only ACTIVE adsets
budget = sum(adset.daily_budget / 100 for adset in adsets if adset.status == 'ACTIVE')
```

### Revenue Cache
After fetching, saves `/tmp/tracker_cache_{account_id}_{date}.json`:
```json
{
  "campaign_id": {
    "spend_1d": 5000, "rev_1d": 8000,
    "spend_7d": 30000, "rev_7d": 45000
  }
}
```
This is used by `campaign_tracker_reports.py` for accurate ROAS calculations.

### Column Headers (26 columns, A→Z)
| Col | Header | Source |
|-----|--------|--------|
| A | Account name | API |
| B | Campaign name | API |
| C | Attribution setting | Derived from attribution_spec |
| D | Status | active/inactive |
| E | Start date | start_time formatted DD-Mon-YYYY |
| F | Day Taken | (report_date - start_date).days + 3 |
| G | Amount spent (INR) | insights.spend |
| H | Roas 1 Day | 1d_click window, today only |
| I | Roas 2 days | 1d_click window, last 2 days |
| J | Roas 3 days | 1d_click window, last 3 days |
| K | Roas 7 days | 7d_click window, last 7 days |
| L | Budget | daily_budget / 100 or sum adsets |
| M | Impressions | insights.impressions |
| N | Clicks (all) | insights.clicks |
| O | CPM | (spend/impressions)*1000 |
| P | CTR (ALL) | (clicks/impressions)*100 |
| Q | Impressions/Spent | impressions/spend |
| R | Last Edited Date | blank (manual) |
| S | Days | blank (manual) |
| T | campaign_id | API |
| U | Column 1 | `#` + campaign_id |
| V | Audience Exc | blank (manual) |
| W | Type | Sales / Retarget / TOF (derived) |
| X | Segment | audience segment (derived) |
| Y | Product | product name (derived) |
| Z | Category | Skin/Hair/Crystal/etc (derived) |

### Campaign Type Detection (W column)
From `product_catalogue.py → derive_type()`:
- **Retarget**: keywords like `rtg`, `rgt`, `retarget`, `seg`, `visitor`, `impression_`, `atc`, `bof`, `ntn`, `180dp`, `180imp`, `exc30`, `lla`, `lookalike`
- **TOF**: keywords like `tof`, `interest`, `loose`, `broad`, `cold`
- **Sales**: everything else

### Category Detection (Z column)
From `product_catalogue.py → derive_product_and_category()`:
Categories: `Skin`, `Hair`, `Crystal`, `Jewellery`, `Nutraceuticals`, `Fragrance`, `Clocks`, `Frames`, `Mix`, `Unmapped`

Key product keywords → category mappings:
- am_pm, ampm, sunkissed, goat_milk, triderma, 24k, pitglow → Skin
- xtremehair, phusphus, hairmist → Hair
- pyrite, selenite, crystal, bracelet, hourglass, richie_rich, horseclock → Crystal
- wanda, jewellery, gold → Jewellery
- charbigone, berberine → Nutraceuticals
- fragrance, perfume, edp → Fragrance

### Write Logic
```
ALWAYS: delete tab → recreate → write fresh (no append)
This prevents ALL merge conflicts and duplicate rows.
```

---

## 📊 Report 2: Campaign Tracker Reports (Category/Audience/Product)

### What it is
A single tab `📊 Reports DD MMM YY` with 4 sections stacked vertically, built from the tracker tabs.

### Script
`campaign_tracker_reports.py --date YYYY-MM-DD`

### When it runs
4:30 AM IST (after builder)

### Data Source
Reads from `SM DD MMM YY`, `SML DD MMM YY`, `NBP DD MMM YY` tabs.
Revenue cache `/tmp/tracker_cache_*.json` provides accurate per-window ROAS.

### ROAS Formula (Weighted)
```python
# All ROAS in reports = weighted by spend
weighted_roas = sum(revenue_Nd for all campaigns) / sum(spend_Nd for all campaigns)
# NOT simple average — spend-weighted to reflect true ROAS
```

### De-duplication
Each row is de-duped by campaign_id before aggregation. First row wins.

### Section 1: Category Budget
- Rows: Each Category × Type (Sales/Retarget) combination
- Columns: Category | Type | Budget | Δ Budget | Camps | Δ Camps | 1D ROAS | Δ 1D | 7D ROAS | Δ 7D | Spend | Δ Spend
- Δ = comparison vs previous day
- CATEGORY_ORDER: Skin → Hair → Crystal → Jewellery → Nutraceuticals → Fragrance → Clocks → Frames → Mix → Unmapped

### Section 2: Audience Budget
- Rows: Each Audience Segment × Category combination
- Δ Spend + Δ 7D ROAS vs previous day
- AUDIENCE_ORDER: Lifetime audience → Loose → Sale 30 → Visitor 180/30 → Impression 30/7 → NTN segments → Unmapped

### Section 3: Product Budget
- Product × Audience per portal

### Section 4: Unmapped Campaigns
- All campaigns where Category/Audience/Product = "Unmapped"
- For cleanup — add keywords to product_catalogue.py

### Write Logic
```
ALWAYS: delete tab → recreate fresh
No clear + rewrite — delete prevents merge conflicts from old formatting.
```

---

## 🔴 Report 3: Closing Watchlist

### What it is
A tab `🔴 Closing DD MMM YY` that accumulates 4 updates per day (10AM, 12PM, 3PM, 6PM). Each update appends below the previous, showing progression and closed camps throughout the day.

### Script
`closing_watchlist.py`

### When it runs
10AM (first run — recreates tab), 12PM, 3PM, 6PM (append below)

### Data Source
100% live Meta API — fetches for current date, so ROAS reflects what's happening NOW.

### ROAS Attribution Used
**`value` key from purchase_roas** (default attribution = click + view + engaged view)
This matches what's shown in the Campaign Tracker sheet.
NOT `1d_click` alone — that gives lower/different numbers.

### Camp Age Calculation
```python
age_days = (today - campaign.start_time.date()).days
# Buckets:
# Day 0: started today
# Day 1: started yesterday
# Day 2: started 2 days ago
# Day 3: started 3 days ago
# Day 4–7: started 4-7 days ago
# Day 7+: older (shown as "Day 7+ (Nd)")
```

### Snapshot System
- **Per-run snapshot:** `/tmp/closing_snapshot_{date}.json` — updated every run, used to detect "closed since last run"
- **Morning snapshot:** `/tmp/closing_morning_{date}.json` — saved ONCE at 10AM, used for "total closed since morning"

### Section 1: 🚨 Critical (Under 1x ROAS)
- All ACTIVE campaigns with spend today and ROAS < 1.0
- Bucket summary table: count, budget, % spent, zero-ROAS camps, zero-ROAS budget
- Detailed list per bucket with:
  - Today ROAS | Yesterday ROAS | 7D ROAS
  - ⚠️ Flag: `✅ Was X.Xx yday` (was good yesterday, bad day today — maybe don't close)
  - ⚠️ Flag: `📈 Max X.Xx` (has high historical ROAS — maybe don't close)

### Section 2: ⚠️ Warning (Age-Based Thresholds)
Different thresholds per age — the older the camp, the higher the standard:
| Age | Threshold | Rationale |
|-----|-----------|-----------|
| Day 0 | < 1.0x | New — just launched, give some time |
| Day 1 | < 1.5x | Starting to expect results |
| Day 2 | < 1.5x | Should be performing by now |
| Day 3 | < 1.5x | 3 days in — needs to work |
| Day 4–7 | < 2.0x | Mature camp — higher bar |
| Day 7+ | < 2.0x | Old camp — must earn its budget |

### Section 3: 📊 Budget by Day × Type
- Rows: Each age bucket
- Columns: Total Budget | Total Spend | Overall ROAS | Sales Budget | Sales ROAS | Retarget Budget | Retarget ROAS | TOF Budget | TOF ROAS
- Shows where budget concentration is and which segment type is underperforming

### Closed Camps Sections (runs 2/3/4 only)
1. **Closed since last run:** Camps in previous snapshot not currently spending
2. **Total closed since 10AM:** Cumulative all-day view with segmentation (Sales/Retarget/TOF breakdown)

### Portal
SM only (Studd Muffyn) — NBP is Madhav Ji's territory (never touch). DS accounts excluded.

---

## ✂️ Report 4: Live Closing Recommendations

### What it is
Appended section in `📊 Reports DD MMM YY` tab. Two lists:
1. Full watchlist (all under threshold)
2. Recommended to close (₹2L budget cap)

### Script
`closing_camps_report.py --date YYYY-MM-DD`

### Closing Budget Logic (₹2L daily cap)
```
Total cap: ₹2,00,000

Allocation:
- Early group (Day 0 + Day 1 + Day 2): 40% = ₹80,000 | threshold: ROAS < 1.0
- Mid group (Day 3–7):                 30% = ₹60,000 | threshold: ROAS < 1.0
- Old group (Day 7+):                  30% = ₹60,000 | threshold: ROAS < 1.5

Within each group: oldest first, then lowest ROAS
```

### Rationale
- Day 7+ under 1.5x gets priority because older camps have had more time to prove themselves
- Day 0 under 1.0x — close with caution (might just be early in the day)
- Green 7D ROAS (≥1.5x) = historically good camp, maybe pause instead of close

---

## 🔴 Report 5: Live Camp Monitor

### What it is
A tab `🔴 Live Monitor` that's completely overwritten every 3 hours with live data.

### Script
`live_camp_monitor.py` (every 3 hrs) + `live_camp_monitor.py --closing --notify` (1PM)

### When it runs
3:30 AM, 6:30 AM, 9:30 AM, 12:30 PM, 3:30 PM, 6:30 PM IST

### Sections
1. Age bucket summary (count, budget, spend, avg ROAS per bucket)
2. All camps by age with Δ ROAS vs previous 3-hour check
3. ✂️ Recommended to close (same ₹2L logic as above)

### Delta ROAS
Compares current ROAS vs `/tmp/live_monitor_snapshot.json` from previous run.
`▲` = improved, `▼` = declined.

### 1PM WA Alert
Sends closing report to Pulkit's WhatsApp (+919517744959) with:
- Total running camps, spend, avg ROAS
- Camps by threshold group
- Link to sheet

---

## 🎨 Report 6: Creative Dashboard

### What it is
A web dashboard at `https://desistuddmuffyn.in/creative-dashboard` showing ad-level creative performance by category and creative type.

### Script
`build_creative_dashboard.py` (local) → deploys to EC2
EC2: `/home/ec2-user/antriksh-ui/creative_dashboard_refresh.py`

### When it runs
5:00 AM IST daily (EC2 cron)

### Creative Type Classification (from ad name)
```python
Paras       → ad name contains 'paras'
Partnership → ad name contains 'partnership', 'collab', 'influencer', 'aliabhat'
Catalogue   → ad name contains 'catalog', 'dpa', 'carousel'
Static      → ad name contains 'static', 'image', 'banner'
Motion      → ad name contains 'reel', 'video', 'motion', 'inde'
Testing     → ad name starts with 'testing_' or 'test_'
Others      → everything else
```

### Data Structure
```
categories: {
  Skin: {
    yesterday: {
      total_spend, total_revenue, total_roas, total_purchases,
      ads: [...],
      creative_type_breakdown: {
        Paras: {ads, spend, revenue, roas, purchases}
        ...
      }
    }
    today: { same structure }
  }
  Hair, Crystals, Fragrance, Nutraceuticals, Other: { same }
}
```

### Category Detection (from campaign name)
- Hair → hair, phusphus, xtremehair, hairmist
- Skin → skin, am_pm, sunkissed, goat_milk, triderma, trifecta
- Crystals → crystal, selenite, pyrite, bracelet, hourglass, peacock, richie_rich
- Fragrance → fragrance, perfume, edp, charbigone, nazar, berberine
- Nutraceuticals → nutra, supplement, vitamin, berber
- Other → everything else

### EC2 Deployment
```bash
# Local build + deploy
python3 build_creative_dashboard.py
scp creative_dashboard_YYYY-MM-DD.html ec2-user@13.126.250.175:/tmp/
ssh ec2-user@13.126.250.175 "sudo cp /tmp/creative_dashboard_YYYY-MM-DD.html /usr/share/nginx/html/creative-dashboard.html"

# EC2 also has its own refresh script at
# /home/ec2-user/antriksh-ui/creative_dashboard_refresh.py
# Reads .env from /home/ec2-user/antriksh/.env
# Writes to /home/ec2-user/antriksh-ui/data/creative_dashboard.json
# Served by Flask app at /api/creative-dashboard-data
```

### ⚠️ Known Issue
EC2 Meta token expires periodically (every ~6 months). When it does:
1. Update token in `/home/ec2-user/antriksh/.env` on EC2
2. Re-run refresh script

---

## 📊 Report 7: Category Performance Dashboard (NTN Dashboard)

### What it is
EC2 web dashboard at `https://desistuddmuffyn.in` showing daily ROAS, orders, revenue trends.

### Script
`auto_rebuild_dashboard.py`

### When it runs
Daily as part of morning pipeline

### Data Source
Google Sheet `1squ0JkqwiyFwIMRmqWc3q_AWHQtihn5o4dbDGyv7sAY`
Tab: `Raw` (main data tab with dates as rows)
Tab: `KPI Daily` (portal-level daily KPIs)

---

## 🔑 Core API Patterns

### Meta Insights Call
```python
requests.get(f"https://graph.facebook.com/v19.0/{account_id}/insights", params={
    'level': 'campaign',
    'fields': 'campaign_id,campaign_name,spend,purchase_roas,impressions,clicks',
    'action_attribution_windows': json.dumps(['1d_click']),  # or ['7d_click']
    'time_range': json.dumps({'since': 'YYYY-MM-DD', 'until': 'YYYY-MM-DD'}),
    'filtering': json.dumps([{'field': 'spend', 'operator': 'GREATER_THAN', 'value': '0'}]),
    'limit': 500,
    'access_token': TOKEN
})
```

### ROAS Extraction
```python
def extract_roas(purchase_roas_field, key='value'):
    # purchase_roas is a list: [{'action_type': 'omni_purchase', 'value': '1.23', '1d_click': '0.89', '7d_click': '1.45'}]
    for item in purchase_roas_field:
        if isinstance(item, dict):
            v = item.get(key) or item.get('value', 0)
            return round(float(v or 0), 2)
    return 0.0

# Use 'value' for consistency with what shows in Meta Ads Manager
# Use '1d_click' for strict same-day click attribution
```

### Pagination
```python
def paginate(endpoint, params):
    results = []
    params['access_token'] = TOKEN
    r = requests.get(f"{GRAPH}/{endpoint}", params=params, timeout=30)
    data = r.json()
    results.extend(data.get('data', []))
    while data.get('paging', {}).get('next'):
        r = requests.get(data['paging']['next'], timeout=30)
        data = r.json()
        results.extend(data.get('data', []))
    return results
```

### Rate Limiting
```python
if data['error']['code'] == 17:  # rate limit
    time.sleep(60)
    continue
```

### Google Sheets Write (safe pattern)
```python
# ALWAYS: delete + recreate — never clear + rewrite
# Clear leaves old merged cells which conflict with new merge requests

if tab_name in existing_titles:
    sh.del_worksheet(sh.worksheet(tab_name))
ws = sh.add_worksheet(title=tab_name, rows=500, cols=20)

# Write
ws.update(range_name='A1', values=data_rows, value_input_option='USER_ENTERED')

# Format in batches of 200 to avoid API limits
for chunk in range(0, len(fmt_reqs), 200):
    sh.batch_update({'requests': fmt_reqs[chunk:chunk+200]})
```

---

## 📐 Key Business Rules

### Budget Rules
- **Total daily push budget:** ~₹3,00,000
- **Target closing budget:** ₹2,00,000 per day
- **Allocation:** Day 0+1+2 = 40% | Day 3-7 = 30% | Day 7+ = 30%

### ROAS Thresholds by Age
| Age | Close if ROAS < |
|-----|----------------|
| Day 0 | 1.0x (end of day) |
| Day 1–2 | 1.5x |
| Day 3–7 | 1.5x |
| Day 7+ | 2.0x |

### ROAS Thresholds by Category (Nia's directives)
- Crystals: close below 1.0x
- Skin: close below 1.5x
- Hair: close below 1.5x
- Fragrance/other: close below 1.5x

### Never Close
- NBP accounts — Madhav Ji's territory
- DS accounts (ds_ prefix) — DS team managed
- SM Credit Line 05 — excluded per Nia
- SM Crystals account — excluded per Nia
- Camps with high 7D ROAS having one bad day — pause, don't close

### Success Rate Definition
"Budget success" = campaigns with 1D ROAS ≥ 1.5x / total Day-0 budget
Target: improve day-0 success rate (currently 4-8%)

---

## 🗂️ Env Variables Required
```bash
META_ACCESS_TOKEN=...
GOOGLE_SERVICE_ACCOUNT_FILE=google-service-account.json
SHOPIFY_ACCESS_TOKEN=...        # SM
SHOPIFY_STORE_URL=studd-muffyn.myshopify.com
SHOPIFY_ACCESS_TOKEN_SML=...
SHOPIFY_STORE_URL_SML=studdmuffynlife.myshopify.com
SHOPIFY_ACCESS_TOKEN_NBP=...
SHOPIFY_STORE_URL_NBP=472d21.myshopify.com
SM_FRAGRANCE_01=act_466922745634023
SM_SKIN=act_578075381064759
# ... all other account IDs
```

---

## 🚨 Common Pitfalls & Fixes

| Problem | Cause | Fix |
|---------|-------|-----|
| Duplicate rows in tracker tabs | Multiple accounts write to same tab via append | Collect all accounts into dict{cid:row}, write once |
| `mergeCells conflict` error | Clearing tab doesn't remove merge formatting | Delete tab + recreate (never clear + rewrite) |
| ROAS shows 0.4x but manually see 2.1x | Using `1d_click` key vs `value` key | Use `value` key for default attribution |
| Creative dashboard empty | EC2 Meta token expired | Update token in `/home/ec2-user/antriksh/.env` |
| `grid limits exceeded` | Sheet has fewer rows than append target | `ws.add_rows(needed - current + 100)` before writing |
| Missing campaigns in SML tab | Campaign started same day, not yet in 1D window | Normal — appears next day |

---

## 📱 Contacts
- **Pulkit Sharma** (owner): +919517744959 — WhatsApp
- **Kashish** (ops): +917889166849 — daily reports recipient
- **Tajinder** (team): +919592573796 — all reports approved
- **Nia Khanna**: +919915868288 — Meta Ads reports approved
- **Harpreet Sahota**: +919988090074 — Meta Ads reports approved
- **Sam/Navdeep**: +918283901380 — NTN team, audience/overlap reports
