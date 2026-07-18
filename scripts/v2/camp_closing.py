#!/usr/bin/env python3
"""
camp_closing.py — REPORT 2: campaign closing engine. Ranks every live campaign
by kill signal, writes the sheet + WhatsApp digest, and PAUSES the ones that hit
the kill matrix.

THIS PAUSES REAL CAMPAIGNS. Rails, all of which must pass:
  * portal whitelist — SM / SML / NBP only. BP, PU, TransfersX are never touched.
  * time window      — 06:30-23:45 IST (nothing gets killed overnight on thin data).
  * once per day     — a campaign is auto-paused at most once per IST day.
  * status ACTIVE    — already-paused campaigns are skipped.
  * circuit breaker  — at most MAX_PAUSES_PER_RUN in one run; if the matrix
                       suddenly matches everything, that's a bug, not a signal.
Every decision (paused or not) is archived to camp_closing_log, and --dry-run
gives the full report with zero API writes.

KILL MATRIX — spend% of daily budget × ROAS. Validated against this repo's own
snapshot history via success_lookup.py: at >=50% spend with ROAS <=0.6, 1-4% of
campaigns ever finish the day at 1.6; at ROAS >=1.6 76-100% do.

Usage:
  python3 scripts/v2/camp_closing.py --db state/camp_snapshots.db --dry-run
  META_ACCESS_TOKEN=... python3 scripts/v2/camp_closing.py --db ... --sheet <id> --whatsapp
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from portal_hourly import portal_of  # noqa: E402
from success_lookup import TARGET_ROAS, TARGET_ROAS_2, build_both  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
API = 'https://graph.facebook.com/v19.0'

ALLOWED_PORTALS = {'SM', 'SML', 'NBP'}
MAX_PAUSES_PER_RUN = 25
WINDOW_START = (6, 30)
WINDOW_END = (23, 45)

# (min spend% of budget, max ROAS) — tightest band first so the log names the
# strongest signal that fired.
KILL_MATRIX = [(50, 0.5), (40, 0.4), (35, 0.3), (30, 0.2), (25, 0.1)]

SCALE_SPEND_PCT, SCALE_ROAS = 50, 2.0

# REVIEW band: deep into budget, and history says campaigns in this state
# essentially never finish at target. Reported, never auto-paused.
REVIEW_SPEND_PCT, REVIEW_SUCCESS_PCT = 50, 5.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS camp_closing_log (
  day           TEXT,
  ts            TEXT,
  campaign_id   TEXT,
  campaign_name TEXT,
  account_name  TEXT,
  portal        TEXT,
  daily_budget  REAL,
  spend         REAL,
  spend_pct     REAL,
  roas          REAL,
  roas_3h       REAL,
  marginal_3h   REAL,
  success_rate  REAL,
  success_rate_21 REAL,
  new_or_reactive TEXT,
  status        TEXT,
  rule          TEXT,
  verdict       TEXT,
  paused        INTEGER DEFAULT 0,
  dry_run       INTEGER DEFAULT 0,
  error         TEXT,
  PRIMARY KEY (day, campaign_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_closing_day ON camp_closing_log(day);
"""


def matrix_hit(spend_pct: float, roas: float):
    for band, roas_max in KILL_MATRIX:
        if spend_pct >= band and roas <= roas_max:
            return f'{band}%spend_ROAS<={roas_max}'
    return None


def in_window(now: datetime) -> bool:
    cur = now.hour * 60 + now.minute
    return (WINDOW_START[0] * 60 + WINDOW_START[1]) <= cur <= (WINDOW_END[0] * 60 + WINDOW_END[1])


def value_at_or_before(con, cid: str, slot: str):
    """Latest snapshot for `cid` at or before `slot`. Cron gaps mean the exact
    3-hours-ago slot often doesn't exist, so we take the newest one before it
    rather than reporting no history."""
    return con.execute(
        "SELECT spend, revenue, roas, hour_slot FROM campaign_hourly_snapshots "
        "WHERE campaign_id=? AND hour_slot<=? ORDER BY hour_slot DESC LIMIT 1",
        (cid, slot)).fetchone()


def pause_campaign(cid: str, token: str) -> tuple[bool, str]:
    """POST status=PAUSED. Returns (ok, error). Needs ads_management scope —
    a read-only token fails here with code 200/copyright-style permission errors."""
    data = urllib.parse.urlencode({'status': 'PAUSED', 'access_token': token}).encode()
    req = urllib.request.Request(f'{API}/{cid}', data=data, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.load(r)
        return (bool(body.get('success', True)), '')
    except urllib.error.HTTPError as e:
        return (False, e.read().decode()[:300])
    except Exception as e:
        return (False, str(e)[:300])


def build_first_activity(con) -> dict:
    """campaign_id -> first IST day the campaign spent anything.

    'new' means first activity is today; anything else that is live today has
    been switched back on, which is a different risk profile — a reactivated
    campaign carries learning history, a genuinely new one doesn't.
    """
    horizon = con.execute(
        "SELECT MIN(substr(hour_slot,1,10)) FROM campaign_hourly_snapshots").fetchone()[0]
    out = {}
    for cid, d in con.execute(
            "SELECT campaign_id, MIN(substr(hour_slot,1,10)) FROM campaign_hourly_snapshots "
            "WHERE spend > 0 GROUP BY campaign_id"):
        # A campaign first seen on the DB's own first day may well have been
        # running before history began — unknowable, so don't claim 'new'.
        out[cid] = None if d == horizon else d
    return out


def collect(con, day: str, lk16, lk21, first_activity: dict,
            as_of: str | None = None) -> list[dict]:
    """Snapshot per campaign at `as_of` (default: the latest slot today),
    enriched with 3h history + success rate.

    as_of exists for backtesting: evaluating only the final slot of a past day
    tells you nothing, because by then the operator has already paused the bad
    campaigns by hand. Point it at a mid-morning slot to see what the engine
    would have caught live.
    """
    if as_of:
        latest = con.execute(
            "SELECT MAX(hour_slot) FROM campaign_hourly_snapshots WHERE hour_slot<=? "
            "AND hour_slot LIKE ?", (as_of, day + '%')).fetchone()[0]
    else:
        latest = con.execute(
            "SELECT MAX(hour_slot) FROM campaign_hourly_snapshots WHERE hour_slot LIKE ?",
            (day + '%',)).fetchone()[0]
    if not latest:
        return []
    slot_3h = (datetime.strptime(latest, '%Y-%m-%d %H:00').replace(tzinfo=IST)
               - timedelta(hours=3)).strftime('%Y-%m-%d %H:00')

    rows = con.execute(
        "SELECT campaign_id, campaign_name, account_name, status, "
        "       COALESCE(daily_budget,0), COALESCE(spend,0), COALESCE(revenue,0), "
        "       COALESCE(roas,0), age_hours, objective "
        "FROM campaign_hourly_snapshots WHERE hour_slot=?", (latest,)).fetchall()

    out = []
    for cid, name, acct, status, budget, spend, rev, roas, age, obj in rows:
        portal = portal_of(acct)
        spend_pct = (spend / budget * 100) if budget else 0.0
        prev = value_at_or_before(con, cid, slot_3h)
        roas_3h = prev[2] if prev else None
        marginal = None
        if prev:
            d_spend, d_rev = spend - prev[0], rev - prev[1]
            if d_spend > 0:
                marginal = round(d_rev / d_spend, 2)
        # Momentum-aware where the 3D cell is solid, 2D otherwise (flagged '*').
        rate, fb16 = lk16.rate_3d(spend_pct, roas, marginal or 0)
        rate21, fb21 = lk21.rate_3d(spend_pct, roas, marginal or 0)
        nor = ''
        fa = first_activity.get(cid)
        if fa:
            nor = 'new' if fa == day else 'reactivated'

        rule = matrix_hit(spend_pct, roas) if status == 'Active' and budget > 0 else None
        # The matrix only fires below ROAS 0.5, but history shows campaigns at
        # 80%+ spend with ROAS 0.7-1.2 also essentially never recover. Those are
        # surfaced as REVIEW — reported and ranked, never auto-paused, because
        # widening the kill thresholds is the operator's call, not this script's.
        doomed = (status == 'Active' and not rule and spend_pct >= REVIEW_SPEND_PCT
                  and rate is not None and rate <= REVIEW_SUCCESS_PCT)
        if rule and portal in ALLOWED_PORTALS:
            verdict = 'PAUSE'
        elif rule:
            verdict = 'PAUSE (not whitelisted)'
        elif doomed:
            verdict = 'REVIEW'
        elif spend_pct >= SCALE_SPEND_PCT and roas >= SCALE_ROAS:
            verdict = 'SCALE'
        elif status == 'Active' and spend_pct >= 25 and roas < 1.0:
            verdict = 'WATCH'
        elif status != 'Active':
            # Already off. Labelling these 'OK' alongside healthy live campaigns
            # made a closed loser read like a pass.
            verdict = 'CLOSED'
        else:
            verdict = 'OK'

        out.append({
            'campaign_id': cid, 'campaign_name': name or '', 'account_name': acct or '',
            'portal': portal or '—', 'status': status or 'Active', 'objective': obj or '',
            'age_hours': age, 'daily_budget': budget, 'spend': spend, 'revenue': rev,
            'spend_pct': round(spend_pct, 1), 'roas': round(roas, 2),
            'roas_3h': round(roas_3h, 2) if roas_3h is not None else None,
            'marginal_3h': marginal,
            'success_rate': rate, 'sr_fallback': fb16,
            'success_rate_21': rate21, 'sr21_fallback': fb21,
            'new_or_reactive': nor, 'first_activity': fa or '',
            'rule': rule or '', 'verdict': verdict, 'slot': latest,
        })
    return out


def _fmt_rate(v, fallback):
    """'42%' momentum-aware, '42%*' when it fell back to the momentum-blind 2D cell."""
    return '' if v is None else f"{v}%{'*' if fallback else ''}"


VERDICT_ORDER = {'PAUSE': 0, 'PAUSE (not whitelisted)': 1, 'REVIEW': 2,
                 'WATCH': 3, 'SCALE': 4, 'OK': 5, 'CLOSED': 6}

HEADER = ['Portal', 'Account', 'Campaign', 'Status', 'New/Reactivated', 'Budget ₹', 'Spend ₹',
          'Spend %', 'ROAS', 'ROAS 3h ago', 'ROAS last 3h', f'Success @{TARGET_ROAS}',
          f'Success @{TARGET_ROAS_2}', 'Rule Fired', 'Verdict', 'Action Taken', 'Campaign ID']


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default='state/camp_snapshots.db')
    ap.add_argument('--day', default=None)
    ap.add_argument('--dry-run', action='store_true',
                    help='report only — no Meta writes (still logs what it would pause)')
    ap.add_argument('--sheet', default=os.environ.get('LIVE_ROAS_SHEET_ID',
                                                      '1eW2_qPdsKJ8zAV5-hsXA5HtfVH9NwDhQLyHYGKz5hXk'))
    ap.add_argument('--tab', default='Camp Closing')
    ap.add_argument('--sa', default=os.environ.get('GOOGLE_SERVICE_ACCOUNT_FILE',
                                                   'google-service-account.json'))
    ap.add_argument('--whatsapp', action='store_true')
    ap.add_argument('--no-sheet', action='store_true')
    ap.add_argument('--xlsx', default=None, help='also write a styled .xlsx to this path')
    ap.add_argument('--ignore-window', action='store_true',
                    help='bypass the 06:30-23:45 IST gate (testing only)')
    ap.add_argument('--as-of', default=None,
                    help="backtest at a past slot, e.g. '2026-07-17 09:00'")
    args = ap.parse_args()

    now = datetime.now(IST)
    day = args.day or now.strftime('%Y-%m-%d')
    dry = args.dry_run
    token = os.environ.get('META_ACCESS_TOKEN', '')

    if not Path(args.db).exists():
        print(f'FATAL: missing DB {args.db}')
        sys.exit(1)

    lk16, lk21 = build_both(args.db, exclude_day=day)
    print(f'success lookup: {lk16.n_camp_days} camp-days · '
          f'{len(lk16.table)} 2D / {len(lk16.table3d)} 3D cells @ {TARGET_ROAS} · '
          f'{len(lk21.table)} 2D / {len(lk21.table3d)} 3D cells @ {TARGET_ROAS_2}')

    con = sqlite3.connect(args.db)
    con.executescript(SCHEMA)
    first_activity = build_first_activity(con)
    rows = collect(con, day, lk16, lk21, first_activity, as_of=args.as_of)
    if not rows:
        print(f'no snapshots for {day} — nothing to do')
        return

    already = {r[0] for r in con.execute(
        'SELECT campaign_id FROM camp_closing_log WHERE day=? AND paused=1', (day,))}

    window_ok = args.ignore_window or in_window(now)
    if not window_ok:
        print(f'outside pause window ({WINDOW_START[0]:02d}:{WINDOW_START[1]:02d}-'
              f'{WINDOW_END[0]:02d}:{WINDOW_END[1]:02d} IST) — reporting only')

    targets = [r for r in rows if r['verdict'] == 'PAUSE' and r['campaign_id'] not in already]
    breaker = len(targets) > MAX_PAUSES_PER_RUN
    if breaker:
        print(f'⚠️  CIRCUIT BREAKER: {len(targets)} campaigns matched the kill matrix '
              f'(limit {MAX_PAUSES_PER_RUN}). Pausing nothing — investigate before re-running.')

    paused, failed = [], []
    ts = now.isoformat(timespec='seconds')
    for r in rows:
        action = ''
        err = ''
        is_target = r in targets
        if r['verdict'] == 'PAUSE' and r['campaign_id'] in already:
            action = 'already paused today'
        elif is_target and not window_ok:
            action = 'deferred (outside window)'
        elif is_target and breaker:
            action = 'blocked (circuit breaker)'
        elif is_target and dry:
            action = 'WOULD PAUSE (dry-run)'
        elif is_target:
            if not token:
                action, err = 'FAILED (no META_ACCESS_TOKEN)', 'token missing'
            else:
                ok, err = pause_campaign(r['campaign_id'], token)
                action = 'PAUSED' if ok else 'FAILED'
                (paused if ok else failed).append(r)
                time.sleep(0.3)
        r['action'] = action
        r['error'] = err
        con.execute(
            'INSERT OR REPLACE INTO camp_closing_log (day, ts, campaign_id, campaign_name, '
            'account_name, portal, daily_budget, spend, spend_pct, roas, roas_3h, marginal_3h, '
            'success_rate, success_rate_21, new_or_reactive, status, rule, verdict, paused, dry_run, error) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (day, ts, r['campaign_id'], r['campaign_name'], r['account_name'], r['portal'],
             r['daily_budget'], r['spend'], r['spend_pct'], r['roas'], r['roas_3h'],
             r['marginal_3h'], r['success_rate'], r['success_rate_21'],
             r['new_or_reactive'], r['status'], r['rule'], r['verdict'],
             1 if action == 'PAUSED' else 0, 1 if dry else 0, err))
    con.commit()

    rows.sort(key=lambda r: (VERDICT_ORDER.get(r['verdict'], 9), -r['spend']))
    n_pause = sum(1 for r in rows if r['verdict'] == 'PAUSE')
    n_watch = sum(1 for r in rows if r['verdict'] == 'WATCH')
    n_scale = sum(1 for r in rows if r['verdict'] == 'SCALE')
    n_review = sum(1 for r in rows if r['verdict'] == 'REVIEW')

    print(f"\n{day} slot {rows[0]['slot']} · {len(rows)} campaigns · "
          f"{n_pause} PAUSE, {n_review} REVIEW, {n_watch} WATCH, {n_scale} SCALE")
    print(f"{'VERDICT':22} {'PORTAL':6} {'SPEND%':>7} {'ROAS':>6} {'3hMARG':>7} {'SUCC':>6}  CAMPAIGN")
    for r in rows[:20]:
        sr = f"{r['success_rate']:.0f}%" if r['success_rate'] is not None else '—'
        mg = f"{r['marginal_3h']:.2f}" if r['marginal_3h'] is not None else '—'
        print(f"{(r['verdict'] + (' → ' + r['action'] if r['action'] else '')):22.22} "
              f"{r['portal']:6} {r['spend_pct']:>7.1f} {r['roas']:>6.2f} {mg:>7} {sr:>6}  "
              f"{r['campaign_name'][:44]}")
    if not dry:
        print(f"\npaused {len(paused)}, failed {len(failed)}")
        for r in failed:
            print(f"  FAILED {r['campaign_id']} {r['campaign_name'][:40]}: {r['error'][:160]}")

    if not args.no_sheet:
        import gspread
        values = [[
            r['portal'], r['account_name'], r['campaign_name'], r['status'],
            r['new_or_reactive'], round(r['daily_budget']), round(r['spend']),
            r['spend_pct'], r['roas'],
            r['roas_3h'] if r['roas_3h'] is not None else '',
            r['marginal_3h'] if r['marginal_3h'] is not None else '',
            _fmt_rate(r['success_rate'], r['sr_fallback']),
            _fmt_rate(r['success_rate_21'], r['sr21_fallback']),
            r['rule'], r['verdict'], r['action'], r['campaign_id'],
        ] for r in rows]
        mode = 'DRY-RUN (no Meta writes)' if dry else 'LIVE auto-pause'
        top = [[f"🛑 CAMP CLOSING — {day} · slot {rows[0]['slot'][-5:]} IST · {mode} · "
                f"{n_pause} PAUSE / {n_review} REVIEW / {n_watch} WATCH / {n_scale} SCALE · "
                f"REVIEW = matrix says keep, history says <{REVIEW_SUCCESS_PCT:.0f}% recover "
                f"(never auto-paused) · "
                f"'Success @{TARGET_ROAS}' = share of past camp-days in the same spend%×ROAS "
                f"band that finished at or above {TARGET_ROAS} "
                f"({lk16.n_camp_days} camp-days, isotonic-fitted; '*' = momentum-blind "
                f"2D fallback) · Meta pixel"]]
        gc = gspread.service_account(filename=args.sa)
        sh = gc.open_by_key(args.sheet)
        try:
            w = sh.worksheet(args.tab)
            w.clear()
        except gspread.WorksheetNotFound:
            w = sh.add_worksheet(title=args.tab, rows=max(len(values) + 40, 200), cols=len(HEADER) + 2)
        w.update(range_name='A1', values=top + [HEADER] + values)
        try:
            w.freeze(rows=2)
            w.format('A1:Q2', {'textFormat': {'bold': True}})
        except Exception:
            pass
        print(f"wrote '{args.tab}' ({len(values)} rows)")

    if args.xlsx:
        from xlsx_out import new_workbook, write_table
        wb = new_workbook()
        mode = 'DRY-RUN (no Meta writes)' if dry else 'LIVE auto-pause'
        NUMFMT = {6: '#,##0', 7: '#,##0', 8: '0.0"%"', 9: '0.00', 10: '0.00', 11: '0.00'}
        CENTER = {1, 4, 5, 8, 9, 10, 11, 12, 13, 15, 16}

        def vals(rs):
            return [[
                r['portal'], r['account_name'], r['campaign_name'], r['status'],
                r['new_or_reactive'], round(r['daily_budget']), round(r['spend']),
                r['spend_pct'], r['roas'],
                r['roas_3h'] if r['roas_3h'] is not None else '',
                r['marginal_3h'] if r['marginal_3h'] is not None else '',
                _fmt_rate(r['success_rate'], r['sr_fallback']),
                _fmt_rate(r['success_rate_21'], r['sr21_fallback']),
                r['rule'], r['verdict'], r['action'], r['campaign_id'],
            ] for r in rs]

        # Sheet 1 — only what needs a decision, ranked hardest-signal first.
        act = [r for r in rows if r['verdict'] in
               ('PAUSE', 'PAUSE (not whitelisted)', 'REVIEW', 'WATCH')]
        ws = wb.create_sheet('Action')
        write_table(
            ws, f'🛑 CAMP CLOSING — {day} · slot {rows[0]["slot"][-5:]} IST · {mode}',
            HEADER, vals(act), verdict_col=14, numfmt=NUMFMT, center_cols=CENTER,
            band=f'NEEDS A DECISION — {n_pause} PAUSE (matrix hit) · {n_review} REVIEW '
                 f'(matrix keeps it, history says it will not recover) · {n_watch} WATCH')

        # Sheets 2-4 — per portal, split at 50% of budget, matching the layout
        # the team already reads in active_camps_by_portal.
        for p in ('SM', 'SML', 'NBP'):
            pr = [r for r in rows if r['portal'] == p]
            if not pr:
                continue
            ws = wb.create_sheet(p)
            hi = [r for r in pr if r['spend_pct'] >= 50]
            lo = [r for r in pr if r['spend_pct'] < 50]
            nxt = write_table(
                ws, f'{p} — Campaigns ({day})', HEADER, vals(hi), verdict_col=14,
                numfmt=NUMFMT, center_cols=CENTER,
                band=f'SPEND >= 50% OF BUDGET   ({len(hi)} campaigns)')
            write_table(
                ws, '', HEADER, vals(lo), start=nxt, verdict_col=14,
                numfmt=NUMFMT, center_cols=CENTER,
                band=f'SPEND < 50% OF BUDGET   ({len(lo)} campaigns)')

        # Sheet 5 — the success grid itself, so the % in the table is auditable.
        ws = wb.create_sheet('Success Grid 1.6')
        rbs = sorted({rb for _sb, rb in lk16.table}, key=lambda b: b[0])
        sbs = sorted({sb for sb, _rb in lk16.table}, key=lambda b: b[0])
        grid_hdr = ['spend %'] + [f'{rb[0]:.1f}+' for rb in rbs]
        grid = [[f'{sb[0]:.0f}%'] + [(f'{lk16.table[(sb, rb)]}%' if (sb, rb) in lk16.table else '')
                                     for rb in rbs] for sb in sbs]
        write_table(
            ws, f'SUCCESS RATE @ {TARGET_ROAS} — rows = % of daily budget spent, '
                f'cols = current ROAS, cell = share of past camp-days in that state '
                f'that finished the day at or above {TARGET_ROAS}',
            grid_hdr, grid, center_cols=set(range(1, len(grid_hdr) + 1)),
            band=f'{lk16.n_camp_days} camp-days of history · isotonic-fitted so rates '
                 f'never fall as ROAS rises · blank = under {10} samples')

        Path(args.xlsx).parent.mkdir(parents=True, exist_ok=True)
        wb.save(args.xlsx)
        print(f'wrote {args.xlsx}')

    if args.whatsapp:
        from wa_notify import send
        lines = [f"CAMP CLOSING — {day} {rows[0]['slot'][-5:]} IST",
                 f"{'DRY-RUN' if dry else 'LIVE'} · {n_pause} to pause, {n_watch} watch, {n_scale} scale"]
        for r in [x for x in rows if x['verdict'] == 'PAUSE'][:8]:
            sr = f", {r['success_rate']:.0f}% recover" if r['success_rate'] is not None else ''
            lines.append(f"• {r['portal']} {r['campaign_name'][:34]} — {r['spend_pct']:.0f}% spent, "
                         f"ROAS {r['roas']:.2f}{sr} → {r['action'] or 'pending'}")
        if not dry and failed:
            lines.append(f"⚠️ {len(failed)} pause call(s) failed — check the sheet")
        sent, fail = send('\n'.join(lines), header='🛑 Camp Closing')
        print(f'whatsapp: {sent} sent, {fail} failed')

    con.close()


if __name__ == '__main__':
    main()
