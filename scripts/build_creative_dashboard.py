#!/usr/bin/env python3
"""
Creative Dashboard — All portals (SM / SML / NBP), all accounts
Pulls ad-level spend + revenue, groups by Creative Type × Product × Portal
Deploys to https://desistuddmuffyn.in/creative-dashboard.html
"""
import requests, json, re, os
from dotenv import dotenv_values
from datetime import datetime, timedelta
from collections import defaultdict

env   = dotenv_values('/Users/pulkitsharma/.openclaw/workspace/.env')
TOKEN = env.get('META_ACCESS_TOKEN')
TODAY = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
print(f'Date: {TODAY}')

ACCOUNTS = [
    (env.get('SM_FRAGRANCE_01'),   'SM'),
    (env.get('SM_SKIN'),           'SM'),
    (env.get('SM_HAIR'),           'SM'),
    (env.get('SM_CRYSTALS'),       'SM'),
    (env.get('SM_PERFUME'),        'SM'),
    (env.get('SM_CREDIT_LINE_05'), 'SM'),
    (env.get('SM_CREDIT_LINE_06'), 'SM'),
    (env.get('SML_SKIN'),          'SML'),
    (env.get('SML_HAIR'),          'SML'),
    (env.get('SML_CRYSTALS'),      'SML'),
    (env.get('SML_CL_06'),         'SML'),
    (env.get('SML_CL_07'),         'SML'),
    (env.get('NBP_SKIN'),          'NBP'),
    (env.get('NBP_HAIR_PERFUME'),  'NBP'),
    (env.get('NBP_CRYSTALS'),      'NBP'),
]

# ── Creative type detection ───────────────────────────────────────────────────
CREATIVE_PATTERNS = [
    ('Paras',       re.compile(r'(?<![a-z])paras(?![a-z])', re.I)),
    ('Partnership', re.compile(r'partnership', re.I)),
    ('Catalogue',   re.compile(r'catal[oa]g(?:ue)?|dpa|carousel', re.I)),
    ('Static',      re.compile(r'(?<![a-z])static(?![a-z])', re.I)),
    ('Motion',      re.compile(r'(?<![a-z])(?:reel|video|motion|inde)(?![a-z])', re.I)),
    ('Testing',     re.compile(r'^test(?:ing)?_', re.I)),
]
TYPES = ['Paras', 'Partnership', 'Motion', 'Static', 'Catalogue', 'Testing', 'Others']
PORTALS = ['SM', 'SML', 'NBP']

def ctype(ad_name):
    for label, pattern in CREATIVE_PATTERNS:
        if pattern.search(ad_name): return label
    return 'Others'

# Product from campaign name
SKIP = {'reel','video','image','static','carousel','clp','dpa','catalogue','catalog','mix',
        'sales','offer','adv','web','conv','wanda','ntn','ds','sml','nbp','sm','brand',
        'r','v2','v3','copy','n','motion','retarget','rtg','rgt','sale','inc180dp','exc180dp'}
PROD_RE = re.compile(r'(?:adv_|tof_|web_|wanda_|ds_)(?:web_|bof_|tof_)?(?:conv_|sale_|sales_|retarget_|rtg_|rgt_)?(?:skin_|hair_|crystal_|neutra_|nutra_|frag_)?(.+?)(?:_\d{6}|_\d{8}|_\d{9}|$)', re.I)

def product(camp_name):
    n = camp_name.lower()
    # Direct keyword detection (more reliable than regex extraction)
    kw_map = [
        (['am_pm','ampm','am pm'],            'AM/PM Booster Kit'),
        (['time_reversal','trifecta'],        'Time Reversal Trifecta'),
        (['triderma','tri_derma'],            'Triderma Bright'),
        (['sunkissed','sun_kissed','sunmousse'],'Sun Kissed Mousse'),
        (['goat_milk','goatmilk','under_eye'],'Under Eye Goat Milk'),
        (['pitglow','pit_glow'],              'Pit Glow Roll On'),
        (['lipbright','lip_bright','lip_reel'],'Lip Bright'),
        (['xtremehair','xtreme_hair'],        'Xtreme Hair Booster'),
        (['phusphus','phus_phus','hairmist','hair_mist'],'Phus Phus / Hair Mist'),
        (['hair_growth','hairkgrowth'],       'Hair Growth Combo'),
        (['charbigone','charbi_gone'],        'Charbi Gone'),
        (['berberine'],                       'Berberine'),
        (['24k','gold_serum'],                '24K Gold Serum'),
        (['dirtoff','dirt_off'],              'Dirt Off Facewash'),
        (['peptide'],                         'Peptide Products'),
        (['richie_rich','richierich','7.*horse','horses_frame'],'Richie Rich / 7 Horses'),
        (['peacock.*frame','frame.*peacock'], 'Peacock Frame'),
        (['selenite.*plate','sunflower'],     'Selenite Plate'),
        (['selenite.*coaster','coaster'],     'Selenite Coaster'),
        (['sleek.*crystal','crystal.*sleek'], 'Sleek Crystal'),
        (['rose.*quartz'],                    'Rose Quartz'),
        (['money.*bowl','geode'],             'Money Bowl'),
        (['hourglass'],                       'Crystal Hourglass'),
        (['pyrite.*bracelet','bracelet.*pyrite'],'Pyrite Bracelet'),
        (['prem.*sutra'],                     'Prem Sutra'),
        (['nazar.*sutra'],                    'Nazar Sutra'),
        (['bracelet'],                        'Crystal Bracelet'),
        (['pyrite'],                          'Pyrite Products'),
        (['selenite'],                        'Selenite Products'),
        (['crystal.*clock','clock'],          'Crystal Clock'),
        (['crystal.*frame','frame'],          'Crystal Frame'),
        (['miniature'],                       'Crystal Miniature'),
        (['owl'],                             'Crystal Owl'),
        (['deer.*plate','deer'],              'Crystal Deer Plate'),
        (['money.*magnet'],                   'Money Magnet Crystal'),
        (['crystal'],                         'Crystal Products'),
        (['jewellery','jewelry','wanda.*gold','18k.*gold'],'Jewellery'),
        (['astro.*bot','chatbot'],            'Astro Bot'),
        (['astro'],                           'Astro Products'),
        (['fragrance','perfume','edp','oxytocin','serotonin','endorphin'],'Fragrance / EDP'),
        (['solid.*perfume'],                  'Solid Perfume'),
        (['skin_mix','skin.*mix','mix.*skin'],'Skin Mix'),
    ]
    for keywords, label in kw_map:
        for kw in keywords:
            if re.search(kw, n):
                return label
    return 'Other'

def get_revenue(row):
    for a in row.get('action_values', []):
        if a['action_type'] in ('purchase','omni_purchase','offsite_conversion.fb_pixel_purchase'):
            return float(a['value'])
    return 0.0

def get_purchases(row):
    for a in row.get('actions', []):
        if a['action_type'] in ('purchase','omni_purchase','offsite_conversion.fb_pixel_purchase'):
            return float(a['value'])
    return 0.0

# ── Fetch all accounts ────────────────────────────────────────────────────────
all_rows = []
portal_totals = defaultdict(lambda: {'spend':0,'revenue':0,'purchases':0,'ads':0})

for acc_id, portal in ACCOUNTS:
    if not acc_id: continue
    try:
        r = requests.get(
            f'https://graph.facebook.com/v19.0/{acc_id}/insights',
            params={
                'fields': 'ad_id,ad_name,campaign_name,spend,impressions,actions,action_values',
                'time_range': json.dumps({'since': TODAY, 'until': TODAY}),
                'level': 'ad', 'limit': 500,
                'filtering': json.dumps([
                    {'field': 'spend', 'operator': 'GREATER_THAN', 'value': '0'},
                ]),
                'access_token': TOKEN
            }, timeout=60
        )
        data = r.json().get('data', [])
        print(f'  {portal} {acc_id[-6:]}: {len(data)} ads')
        for row in data:
            spend = float(row.get('spend', 0) or 0)
            rev   = get_revenue(row)
            purch = get_purchases(row)
            ad    = row.get('ad_name', '')
            camp  = row.get('campaign_name', '')
            ct    = ctype(ad)
            prod  = product(camp)
            all_rows.append({'portal': portal, 'ad': ad, 'camp': camp,
                              'ct': ct, 'product': prod,
                              'spend': spend, 'revenue': rev, 'purchases': purch})
            portal_totals[portal]['spend']     += spend
            portal_totals[portal]['revenue']   += rev
            portal_totals[portal]['purchases'] += purch
            portal_totals[portal]['ads']       += 1
    except Exception as e:
        print(f'  Error {acc_id}: {e}')

print(f'Total ads: {len(all_rows)}')

# ── Aggregations ──────────────────────────────────────────────────────────────
total_spend = sum(r['spend'] for r in all_rows)
total_rev   = sum(r['revenue'] for r in all_rows)
total_roas  = round(total_rev/total_spend, 2) if total_spend else 0
total_purch = sum(r['purchases'] for r in all_rows)
total_cpr   = round(total_spend/total_purch, 0) if total_purch else 0

# Creative type totals
ct_totals = defaultdict(lambda: {'count':0,'spend':0,'revenue':0,'purchases':0})
for r in all_rows:
    ct_totals[r['ct']]['count']     += 1
    ct_totals[r['ct']]['spend']     += r['spend']
    ct_totals[r['ct']]['revenue']   += r['revenue']
    ct_totals[r['ct']]['purchases'] += r['purchases']

# Product × Portal breakdown
prod_portal = defaultdict(lambda: defaultdict(lambda: {'count':0,'spend':0,'revenue':0}))
prod_totals = defaultdict(lambda: {'spend':0,'revenue':0,'count':0,'purchases':0})
for r in all_rows:
    prod_portal[r['product']][r['portal']]['count']   += 1
    prod_portal[r['product']][r['portal']]['spend']   += r['spend']
    prod_portal[r['product']][r['portal']]['revenue'] += r['revenue']
    prod_totals[r['product']]['spend']     += r['spend']
    prod_totals[r['product']]['revenue']   += r['revenue']
    prod_totals[r['product']]['count']     += 1
    prod_totals[r['product']]['purchases'] += r['purchases']

# Creative type × portal
ct_portal = defaultdict(lambda: defaultdict(lambda: {'count':0,'spend':0,'revenue':0}))
for r in all_rows:
    ct_portal[r['ct']][r['portal']]['count'] += 1
    ct_portal[r['ct']][r['portal']]['spend'] += r['spend']
    ct_portal[r['ct']][r['portal']]['revenue'] += r['revenue']

prod_sorted = sorted(prod_totals.items(), key=lambda x: -x[1]['spend'])

# ── HTML helpers ──────────────────────────────────────────────────────────────
CT_COLORS  = {'Paras':'#7c3aed','Partnership':'#0ea5e9','Catalogue':'#f97316',
               'Static':'#6b7280','Motion':'#06b6d4','Testing':'#f43f5e','Others':'#475569'}
CT_EMOJI   = {'Paras':'🙋','Partnership':'🤝','Catalogue':'🛍️','Static':'🖼️',
               'Motion':'🎬','Testing':'🧪','Others':'📦'}
PORTAL_CLR = {'SM':'#3b82f6','SML':'#22c55e','NBP':'#ef4444'}

def roas_color(roas):
    if roas >= 2.0: return '#22c55e'
    if roas >= 1.2: return '#f59e0b'
    return '#ef4444'

def fmt_roas(sp, rv):
    if sp == 0: return '—'
    r = round(rv/sp, 2)
    return f'<span style="color:{roas_color(r)};font-weight:700">{r}x</span>'

# Portal summary cards
portal_cards_html = ''
for p in PORTALS:
    d = portal_totals[p]
    roas = round(d['revenue']/d['spend'],2) if d['spend'] else 0
    portal_cards_html += f'''
    <div class="portal-card" style="border-top:3px solid {PORTAL_CLR[p]}">
      <div class="portal-label" style="color:{PORTAL_CLR[p]}">{p}</div>
      <div class="portal-ads">{d['ads']} ads</div>
      <div class="portal-spend">₹{d['spend']:,.0f}</div>
      <div class="portal-roas" style="color:{roas_color(roas)}">{roas}x</div>
    </div>'''

# Creative type cards
ct_cards_html = ''
for t in TYPES:
    d = ct_totals[t]
    if not d['count']: continue
    roas = round(d['revenue']/d['spend'],2) if d['spend'] else 0
    # Portal sub-breakdown
    portal_bits = ''
    for p in PORTALS:
        pd = ct_portal[t][p]
        if pd['count']:
            portal_bits += f'<span class="ct-portal" style="background:{PORTAL_CLR[p]}22;color:{PORTAL_CLR[p]}">{p} {pd["count"]}</span>'
    ct_cards_html += f'''
    <div class="ct-card" onclick="filterByType('{t}')" data-type="{t}" style="border-top:3px solid {CT_COLORS[t]}">
      <div class="ct-label">{CT_EMOJI[t]} {t}</div>
      <div class="ct-count">{d["count"]}</div>
      <div class="ct-spend">₹{d["spend"]:,.0f}</div>
      <div class="ct-roas" style="color:{roas_color(roas)}">{roas}x</div>
      <div class="ct-portals">{portal_bits}</div>
    </div>'''

# Product table rows (with SM/SML/NBP columns)
table_rows_html = ''
for pname, ptot in prod_sorted:
    roas = round(ptot['revenue']/ptot['spend'],2) if ptot['spend'] else 0
    portal_cells = ''
    for p in PORTALS:
        pd = prod_portal[pname][p]
        if pd['count']:
            p_roas = round(pd['revenue']/pd['spend'],2) if pd['spend'] else 0
            portal_cells += f'<td><span class="badge" style="background:{PORTAL_CLR[p]}22;color:{PORTAL_CLR[p]};border:1px solid {PORTAL_CLR[p]}44">{pd["count"]}</span><br><small style="color:{roas_color(p_roas)}">{p_roas}x</small></td>'
        else:
            portal_cells += '<td><span style="color:#334155">—</span></td>'
    table_rows_html += f'''<tr data-product="{pname.lower()}" data-ct="all">
      <td class="pname">{pname}</td>
      <td class="total-count">{ptot["count"]}</td>
      {portal_cells}
      <td class="spend-col">₹{ptot["spend"]:,.0f}</td>
      <td class="roas-col">{fmt_roas(ptot["spend"],ptot["revenue"])}</td>
    </tr>'''

# ── HTML ──────────────────────────────────────────────────────────────────────
html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Creative Dashboard — {TODAY}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh}}
.header{{background:linear-gradient(135deg,#1e3a5f,#1e293b);padding:16px 24px;border-bottom:1px solid #1e3a5f;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px}}
.header h1{{font-size:16px;font-weight:700;color:#fff}}
.header p{{color:#94a3b8;font-size:11px;margin-top:2px}}
.header-right .big{{font-size:22px;font-weight:800;color:#f1f5f9;text-align:right}}
.header-right .small{{font-size:11px;color:#64748b;text-align:right}}
.container{{padding:16px 24px;max-width:1400px}}
.scorecards{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:16px}}
.sc{{background:#1e293b;border-radius:10px;padding:14px 16px;border:1px solid #334155}}
.sc-lbl{{font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:.5px}}
.sc-val{{font-size:22px;font-weight:700;margin-top:4px}}
.sc-sub{{font-size:10px;color:#64748b;margin-top:2px}}
.section-title{{font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px;margin-top:16px}}
.portal-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:4px}}
.portal-card{{background:#1e293b;border-radius:10px;padding:12px 14px;border:1px solid #334155;text-align:center}}
.portal-label{{font-size:13px;font-weight:800;margin-bottom:6px}}
.portal-ads{{font-size:11px;color:#64748b}}
.portal-spend{{font-size:16px;font-weight:700;margin-top:3px}}
.portal-roas{{font-size:13px;font-weight:700;margin-top:3px}}
.ct-grid{{display:grid;grid-template-columns:repeat(7,1fr);gap:8px;margin-bottom:4px}}
.ct-card{{background:#1e293b;border-radius:10px;padding:12px 10px;border:1px solid #334155;cursor:pointer;transition:all .15s;text-align:center;user-select:none}}
.ct-card:hover{{background:#253347}}
.ct-card.active{{background:#162032;box-shadow:0 0 0 2px #3b82f6}}
.ct-label{{font-size:10px;font-weight:700;color:#94a3b8;margin-bottom:6px}}
.ct-count{{font-size:20px;font-weight:800;color:#f1f5f9}}
.ct-spend{{font-size:10px;color:#64748b;margin-top:2px}}
.ct-roas{{font-size:11px;font-weight:700;margin-top:4px}}
.ct-portals{{margin-top:6px;display:flex;flex-wrap:wrap;gap:3px;justify-content:center}}
.ct-portal{{font-size:9px;padding:1px 5px;border-radius:8px;font-weight:700}}
.table-section{{background:#1e293b;border-radius:12px;border:1px solid #334155;overflow:hidden;margin-top:12px}}
.table-top{{padding:12px 16px;border-bottom:1px solid #334155;display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}}
.table-top h3{{font-size:11px;font-weight:600;color:#94a3b8;text-transform:uppercase;letter-spacing:.5px}}
.search{{background:#0f172a;border:1px solid #334155;border-radius:6px;padding:5px 10px;color:#e2e8f0;font-size:12px;width:160px}}
.search:focus{{outline:none;border-color:#3b82f6}}
.table-scroll{{overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
thead th{{background:#0f172a;color:#64748b;padding:8px 10px;text-align:center;font-size:10px;text-transform:uppercase;letter-spacing:.4px;white-space:nowrap}}
thead th:first-child,thead th:nth-child(2){{text-align:left}}
tbody tr{{border-bottom:1px solid #1e293b;transition:background .1s}}
tbody tr:hover{{background:#1a2844}}
tbody tr.hidden{{display:none}}
tbody td{{padding:9px 10px;color:#cbd5e1;text-align:center;vertical-align:middle}}
.pname{{text-align:left!important;font-weight:600;color:#f1f5f9;font-size:12px;min-width:140px}}
.total-count{{text-align:left!important;font-weight:800;color:#3b82f6;font-size:15px}}
.spend-col{{color:#94a3b8;white-space:nowrap}}
.badge{{padding:2px 8px;border-radius:10px;font-size:11px;font-weight:700}}
</style>
</head>
<body>
<div class="header">
  <div>
    <h1>🚀 Creative Dashboard — All Portals</h1>
    <p>SM · SML · NBP &nbsp;·&nbsp; {TODAY} &nbsp;·&nbsp; {len(all_rows)} ads &nbsp;·&nbsp; {len(prod_sorted)} products</p>
  </div>
  <div class="header-right">
    <div class="big">₹{total_spend:,.0f}</div>
    <div class="small">Total Spend &nbsp;·&nbsp; {total_roas}x ROAS</div>
  </div>
</div>
<div class="container">

  <div class="scorecards">
    <div class="sc"><div class="sc-lbl">Spend</div><div class="sc-val">₹{total_spend:,.0f}</div><div class="sc-sub">{len(all_rows)} active ads</div></div>
    <div class="sc"><div class="sc-lbl">Revenue</div><div class="sc-val">₹{total_rev:,.0f}</div><div class="sc-sub">Purchase value</div></div>
    <div class="sc"><div class="sc-lbl">ROAS</div><div class="sc-val" style="color:{roas_color(total_roas)}">{total_roas}x</div><div class="sc-sub">Overall</div></div>
    <div class="sc"><div class="sc-lbl">Purchases</div><div class="sc-val">{int(total_purch):,}</div><div class="sc-sub">₹{total_cpr:,.0f} CPR</div></div>
  </div>

  <div class="section-title">Portal Breakdown</div>
  <div class="portal-grid">{portal_cards_html}</div>

  <div class="section-title">Filter by Creative Type</div>
  <div class="ct-grid">
    <div class="ct-card active" onclick="filterByType('all')" data-type="all" style="border-top:3px solid #3b82f6">
      <div class="ct-label">📊 All</div>
      <div class="ct-count">{len(all_rows)}</div>
      <div class="ct-spend">₹{total_spend:,.0f}</div>
      <div class="ct-roas" style="color:{roas_color(total_roas)}">{total_roas}x</div>
      <div class="ct-portals"></div>
    </div>
    {ct_cards_html}
  </div>

  <div class="table-section">
    <div class="table-top">
      <h3>Product × Portal Breakdown</h3>
      <input class="search" placeholder="🔍 Search product..." oninput="searchTable(this.value)">
    </div>
    <div class="table-scroll">
      <table>
        <thead>
          <tr>
            <th style="text-align:left">Product</th>
            <th style="text-align:left">Ads</th>
            <th style="color:{PORTAL_CLR['SM']}">SM</th>
            <th style="color:{PORTAL_CLR['SML']}">SML</th>
            <th style="color:{PORTAL_CLR['NBP']}">NBP</th>
            <th>Spend</th>
            <th>ROAS</th>
          </tr>
        </thead>
        <tbody id="tableBody">{table_rows_html}</tbody>
      </table>
    </div>
  </div>

</div>
<script>
let activeType = 'all';
function filterByType(type) {{
  activeType = type;
  document.querySelectorAll('.ct-card').forEach(c => c.classList.remove('active'));
  event.currentTarget.classList.add('active');
  applyFilters();
}}
function searchTable(q) {{ applyFilters(q); }}
function applyFilters(q) {{
  q = (q !== undefined ? q : document.querySelector('.search').value).toLowerCase();
  document.querySelectorAll('#tableBody tr').forEach(row => {{
    const prodMatch = !q || row.getAttribute('data-product').includes(q);
    row.classList.toggle('hidden', !prodMatch);
  }});
}}
</script>
</body>
</html>'''

outfile = f'/Users/pulkitsharma/.openclaw/workspace/creative_dashboard_{TODAY}.html'
with open(outfile, 'w') as f:
    f.write(html)
print(f'Saved: {outfile}')
