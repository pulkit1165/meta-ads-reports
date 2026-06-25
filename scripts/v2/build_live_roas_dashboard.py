#!/usr/bin/env python3
"""
build_live_roas_dashboard.py — static live dashboard off camp_snapshots.db.

Renders (all client-side from one embedded JSON payload):
  • Report 1 — Alert Monitoring (campaigns currently meeting alert conditions)
  • Report 2 — Hourly ROAS Tracker (current / 1h / 3h / day-start / trend)
  • ROAS Movement (improving vs declining: prev-hour, 3h, day-start)
  • Historical comparisons (hour-vs-hour, same-hour-yesterday, today-vs-yesterday,
    7-day, launch-vs-current) — populate as snapshots accumulate.

Auto-refreshes the page every 15 min (the workflow rebuilds + redeploys hourly).
All per-campaign numbers are Meta PIXEL-attributed.

Usage: python3 scripts/v2/build_live_roas_dashboard.py --db state/camp_snapshots.db --out live-roas/index.html
"""
import argparse
import json
import sqlite3
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))


def bucket_for(roas, is_new):
    if is_new:
        if roas == 0:
            return 0.0
        for t in (0.5, 0.75, 1.0, 1.25, 1.5):
            if roas < t:
                return t
        return None
    for t in (1.25, 1.60):
        if roas < t:
            return t
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default='state/camp_snapshots.db')
    ap.add_argument('--out', default='live-roas/index.html')
    args = ap.parse_args()

    now = datetime.now(IST)
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row

    latest_slot = con.execute("SELECT MAX(hour_slot) FROM campaign_hourly_snapshots").fetchone()[0]
    camps = []
    if latest_slot:
        cur_rows = con.execute("SELECT * FROM campaign_hourly_snapshots WHERE hour_slot=?",
                               (latest_slot,)).fetchall()
        ls = datetime.strptime(latest_slot, '%Y-%m-%d %H:00').replace(tzinfo=IST)
        slot_1h = (ls - timedelta(hours=1)).strftime('%Y-%m-%d %H:00')
        slot_3h = (ls - timedelta(hours=3)).strftime('%Y-%m-%d %H:00')
        slot_yh = (ls - timedelta(hours=24)).strftime('%Y-%m-%d %H:00')
        today = ls.strftime('%Y-%m-%d')

        def roas_at(cid, slot):
            r = con.execute("SELECT roas,spend,revenue FROM campaign_hourly_snapshots "
                            "WHERE campaign_id=? AND hour_slot=?", (cid, slot)).fetchone()
            return r if r else None

        for c in cur_rows:
            cid = c['campaign_id']
            r1 = roas_at(cid, slot_1h)
            r3 = roas_at(cid, slot_3h)
            ryh = roas_at(cid, slot_yh)
            ds = con.execute("SELECT roas,spend,revenue FROM campaign_hourly_snapshots "
                             "WHERE campaign_id=? AND hour_slot LIKE ? ORDER BY hour_slot LIMIT 1",
                             (cid, today + '%')).fetchone()
            launch = con.execute("SELECT roas FROM campaign_hourly_snapshots WHERE campaign_id=? "
                                 "ORDER BY hour_slot LIMIT 1", (cid,)).fetchone()
            la = con.execute("SELECT sent_ts,bucket FROM camp_alert_log WHERE campaign_id=? "
                             "ORDER BY sent_ts DESC LIMIT 1", (cid,)).fetchone()
            is_new = (c['age_hours'] is not None and c['age_hours'] < 24)
            spend_pct = (c['spend'] / c['daily_budget'] * 100) if c['daily_budget'] else 0
            bkt = bucket_for(c['roas'], is_new)
            thr = 30 if is_new else 50
            alerting = bool(bkt is not None and spend_pct >= thr)
            reason = ''
            if alerting:
                reason = (f"{'New' if is_new else 'Mature'}: spend {spend_pct:.0f}% & ROAS "
                          f"{'=0' if bkt == 0 else '<%.2f' % bkt}")
            camps.append({
                'id': cid, 'name': c['campaign_name'], 'account': c['account_name'],
                'account_id': c['account_id'],
                'status': (c['status'] if 'status' in c.keys() and c['status'] else 'Active'),
                'objective': c['objective'], 'age': c['age_hours'], 'budget': c['daily_budget'],
                'spend': c['spend'], 'revenue': c['revenue'], 'roas': c['roas'], 'orders': c['orders'],
                'ctr': c['ctr'], 'cpc': c['cpc'], 'cpm': c['cpm'], 'cpa': c['cpa'],
                'spend_pct': round(spend_pct, 1),
                'roas_1h': r1['roas'] if r1 else None, 'roas_3h': r3['roas'] if r3 else None,
                'roas_daystart': ds['roas'] if ds else None,
                'roas_yest_hr': ryh['roas'] if ryh else None,
                'roas_launch': launch['roas'] if launch else None,
                'spend_1h': r1['spend'] if r1 else None, 'rev_1h': r1['revenue'] if r1 else None,
                'alerting': alerting, 'reason': reason,
                'last_alert': la['sent_ts'] if la else '',
            })
    n_slots = con.execute("SELECT COUNT(DISTINCT hour_slot) FROM campaign_hourly_snapshots").fetchone()[0]
    con.close()

    payload = {
        'generated_at': now.strftime('%d %b %Y, %H:%M:%S IST'),
        'latest_slot': latest_slot or '—', 'n_slots': n_slots,
        'campaigns': camps,
    }
    html = HTML.replace('/*__PAYLOAD__*/', json.dumps(payload))
    import os
    os.makedirs(os.path.dirname(args.out) or '.', exist_ok=True)
    with open(args.out, 'w') as f:
        f.write(html)
    print(f"built {args.out} | slot {latest_slot} | {len(camps)} active campaigns | {n_slots} hourly slots stored")


HTML = r"""<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="900">
<title>Live ROAS Tracker</title>
<style>
:root{--bg:#0b1220;--card:#131c2e;--mut:#8aa;--bd:#22304a;--g:#3fb950;--r:#f85149;--y:#d29922}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#e6edf3;font:13px/1.4 -apple-system,Segoe UI,Roboto,Helvetica,Arial}
header{padding:12px 16px;background:#0d1730;border-bottom:1px solid var(--bd);position:sticky;top:0;z-index:5}
h1{font-size:16px;margin:0 0 2px}.sub{color:var(--mut);font-size:11px}
.tabs{display:flex;gap:6px;padding:8px 16px;flex-wrap:wrap;background:#0d1730;border-bottom:1px solid var(--bd)}
.tab{padding:6px 12px;border:1px solid var(--bd);border-radius:6px;cursor:pointer;color:var(--mut)}
.tab.on{background:#1f6feb22;border-color:#1f6feb;color:#fff}
.wrap{padding:14px 16px}.note{color:var(--mut);font-size:11px;margin:4px 0 10px}
table{width:100%;border-collapse:collapse;font-size:12px}th,td{padding:5px 8px;border-bottom:1px solid var(--bd);text-align:right;white-space:nowrap}
th{position:sticky;top:0;background:#0d1730;cursor:pointer;color:#cbd5e1;text-align:right}
td.l,th.l{text-align:left}tr:hover{background:#ffffff08}
.g{color:var(--g)}.r{color:var(--r)}.y{color:var(--y)}.b{font-weight:700}
.pill{padding:1px 7px;border-radius:9px;font-size:11px}.pi-up{background:#3fb95022;color:var(--g)}.pi-dn{background:#f8514922;color:var(--r)}.pi-st{background:#8aa2;color:var(--mut)}
a{color:#58a6ff;text-decoration:none}.muted{color:var(--mut)}
.kpi{display:inline-block;background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:8px 12px;margin:0 8px 8px 0}
.kpi b{font-size:17px}
</style></head><body>
<header><h1>📡 Live ROAS Tracker <span class="muted" style="font-size:11px">· Meta pixel</span></h1>
<div class="sub">Updated <span id="gen"></span> · slot <span id="slot"></span> · <span id="ns"></span> hourly slots stored · auto-refresh 15 min</div></header>
<div class="tabs" id="tabs"></div>
<div class="wrap" id="view"></div>
<script>
const P=/*__PAYLOAD__*/;
const C=P.campaigns;
gen.textContent=P.generated_at; slot.textContent=P.latest_slot; ns.textContent=P.n_slots;
const f0=x=>x==null?'—':Math.round(x).toLocaleString('en-IN');
const f2=x=>x==null?'—':(+x).toFixed(2);
const pct=(a,b)=> (a==null||b==null||b==0)?null:((a-b)/b*100);
function chgPill(v){if(v==null)return '<span class="pill pi-st">—</span>';
  const c=v>1?'pi-up':v<-1?'pi-dn':'pi-st';const s=v>0?'+':'';return `<span class="pill ${c}">${s}${v.toFixed(1)}%</span>`;}
function trend(c){const v=pct(c.roas,c.roas_1h);if(v==null)return 'Stable';return v>1?'Improving':v<-1?'Declining':'Stable';}
const TABS=[['alerts','🚨 Alert Monitor'],['tracker','📈 Hourly ROAS Tracker'],['move','↕️ ROAS Movement'],['hist','🕓 Historical Compare']];
let cur='tracker';
function tabs(){tabsEl();}
function tabsEl(){document.getElementById('tabs').innerHTML=TABS.map(t=>`<div class="tab ${t[0]==cur?'on':''}" onclick="go('${t[0]}')">${t[1]}</div>`).join('');}
window.go=t=>{cur=t;tabsEl();render();};
let sortKey='spend',sortDir=-1;
function sortBy(k){if(sortKey==k)sortDir*=-1;else{sortKey=k;sortDir=-1;}render();}
window.sortBy=sortBy;
function tbl(cols,rows){
  const head='<tr>'+cols.map(c=>`<th class="${c.l?'l':''}" onclick="sortBy('${c.k}')">${c.t}</th>`).join('')+'</tr>';
  const body=rows.map(r=>'<tr>'+cols.map(c=>`<td class="${c.l?'l':''} ${c.cls?c.cls(r):''}">${c.f(r)}</td>`).join('')+'</tr>').join('');
  return `<table>${head}${body}</table>`;}
function sorted(arr){return [...arr].sort((a,b)=>{let x=a[sortKey],y=b[sortKey];if(x==null)x=-1;if(y==null)y=-1;return (x>y?1:x<y?-1:0)*sortDir;});}
function render(){
 const v=document.getElementById('view');
 if(cur=='alerts'){
   const rows=sorted(C.filter(c=>c.alerting));
   v.innerHTML=`<div class="note">Report 1 — campaigns currently meeting alert conditions (New &lt;24h: spend≥30% &amp; low ROAS · Mature ≥24h: spend≥50% &amp; ROAS&lt;1.60).</div>`+
   `<div class="kpi">Alerting now <b class="r">${rows.length}</b></div>`+
   tbl([{k:'name',t:'Campaign',l:1,f:r=>link(r)},{k:'age',t:'Age(h)',f:r=>r.age??'—'},
     {k:'budget',t:'Budget ₹',f:r=>f0(r.budget)},{k:'spend_pct',t:'Spend %',f:r=>(r.spend_pct||0).toFixed(0)+'%',cls:r=>r.spend_pct>=100?'r':''},
     {k:'roas',t:'ROAS',f:r=>f2(r.roas),cls:r=>'b '+(r.roas>=1.6?'g':r.roas>=1.25?'y':'r')},
     {k:'reason',t:'Alert Reason',l:1,f:r=>r.reason},
     {k:'last_alert',t:'Last Alert Sent',l:1,f:r=>r.last_alert?r.last_alert.replace('T',' ').slice(0,16):'<span class=muted>not yet</span>'}],rows);
 } else if(cur=='tracker'){
   const rows=sorted(C);
   v.innerHTML=`<div class="note">Report 2 — all active campaigns. Δ vs previous hour / 3h ago / day-start. Trend from 1h ROAS move.</div>`+
   tbl([{k:'name',t:'Campaign',l:1,f:r=>link(r)},{k:'account',t:'Account',l:1,f:r=>`<span class=muted>${r.account}</span>`},
     {k:'status',t:'Status',l:1,f:r=>`<span class="pill ${r.status=='Paused'?'pi-dn':'pi-up'}">${r.status}</span>`},
     {k:'roas',t:'ROAS now',f:r=>f2(r.roas),cls:r=>'b '+(r.roas>=2?'g':r.roas>=1.25?'y':'r')},
     {k:'roas_1h',t:'Δ 1h',f:r=>chgPill(pct(r.roas,r.roas_1h))},
     {k:'roas_3h',t:'Δ 3h',f:r=>chgPill(pct(r.roas,r.roas_3h))},
     {k:'roas_daystart',t:'Day-start',f:r=>f2(r.roas_daystart)},
     {k:'spend',t:'Spend ₹',f:r=>f0(r.spend)},{k:'revenue',t:'Rev ₹',f:r=>f0(r.revenue)},
     {k:'orders',t:'Ord',f:r=>r.orders},
     {k:'_tr',t:'Trend',f:r=>{const t=trend(r);return `<span class="pill ${t=='Improving'?'pi-up':t=='Declining'?'pi-dn':'pi-st'}">${t}</span>`;}}],rows);
 } else if(cur=='move'){
   const withPrev=C.filter(c=>c.roas_1h!=null);
   const up=withPrev.filter(c=>pct(c.roas,c.roas_1h)>1).sort((a,b)=>pct(b.roas,b.roas_1h)-pct(a.roas,a.roas_1h));
   const dn=withPrev.filter(c=>pct(c.roas,c.roas_1h)<-1).sort((a,b)=>pct(a.roas,a.roas_1h)-pct(b.roas,b.roas_1h));
   const mv=(arr,title)=>`<h3>${title} (${arr.length})</h3>`+tbl([
     {k:'name',t:'Campaign',l:1,f:r=>link(r)},{k:'roas',t:'Now',f:r=>f2(r.roas),cls:_=>'b'},
     {k:'roas_1h',t:'1h ago',f:r=>f2(r.roas_1h)},{k:'_d',t:'ROAS Δ%',f:r=>chgPill(pct(r.roas,r.roas_1h))},
     {k:'_s',t:'Spend Δ%',f:r=>chgPill(pct(r.spend,r.spend_1h))},{k:'_r',t:'Rev Δ%',f:r=>chgPill(pct(r.revenue,r.rev_1h))},
     {k:'roas_daystart',t:'vs DayStart',f:r=>chgPill(pct(r.roas,r.roas_daystart))}],arr);
   v.innerHTML=`<div class="note">Improving vs declining by ROAS, current hour vs previous hour. (3h &amp; day-start columns shown too.)</div>`+
     mv(up,'📈 Improving')+mv(dn,'📉 Declining');
 } else {
   v.innerHTML=`<div class="note">Historical comparisons populate as hourly snapshots accumulate (need ≥2 hours / prior days for full coverage). Currently storing ${P.n_slots} hourly slots.</div>`+
   tbl([{k:'name',t:'Campaign',l:1,f:r=>link(r)},{k:'roas',t:'Now',f:r=>f2(r.roas),cls:_=>'b'},
     {k:'roas_1h',t:'Prev hr',f:r=>f2(r.roas_1h)},{k:'roas_yest_hr',t:'Same hr yest',f:r=>f2(r.roas_yest_hr)},
     {k:'roas_daystart',t:'Day-start',f:r=>f2(r.roas_daystart)},{k:'roas_launch',t:'Launch',f:r=>f2(r.roas_launch)},
     {k:'_lc',t:'Launch→Now Δ%',f:r=>chgPill(pct(r.roas,r.roas_launch))}],sorted(C));
 }
}
function link(r){return `<a href="https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=${r.account_id||r.account}&selected_campaign_ids=${r.id}" target="_blank">${r.name}</a>`;}
tabsEl();render();
</script></body></html>"""


if __name__ == '__main__':
    main()
