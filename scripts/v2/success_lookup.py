#!/usr/bin/env python3
"""
success_lookup.py — "given a campaign is at X% of budget with ROAS Y right now,
how often did campaigns in that state finish the day at or above target ROAS?"

Built from campaign_hourly_snapshots history. For every completed camp-day we
know the state at each hour and the FINAL ROAS, so each hourly observation
becomes one training example labelled success = (EOD roas >= target).

Three refinements, ported from the EC2 `simple_camp_db.py`:

  1. ISOTONIC SMOOTHING (PAVA). Raw per-bucket rates are noisy — with few
     samples a lower-ROAS bucket can show a HIGHER success rate than a
     higher-ROAS one, which is statistically impossible. Within each spend band
     we force the rate non-decreasing as ROAS rises, pooling adjacent violating
     buckets weighted by sample count. This is what makes fine bands usable:
     the fit borrows strength from neighbours instead of needing every cell to
     be independently well-sampled.
  2. 3D LOOKUP with momentum. Two campaigns at the same (spend%, ROAS) but
     different last-3h ROAS have very different odds — one is picking up
     buyers, the other is dead. Falls back to the 2D cell (marked `*`) when the
     3D cell is thin.
  3. TWO TARGETS. 1.6 (break-even-plus) and 2.1 (scale-worthy) side by side.

DEDUP: a camp-day sitting in the same cell for six hours would otherwise count
six times and drown out short-lived camp-days. Each camp-day contributes at
most one observation per cell.

Usage:
  python3 scripts/v2/success_lookup.py --db state/camp_snapshots.db --show
"""
from __future__ import annotations

import argparse
import sqlite3
from collections import defaultdict

TARGET_ROAS = 1.6
TARGET_ROAS_2 = 2.1
MIN_SAMPLE = 10
MIN_SAMPLE_3D = 5
LAST3H_HOURS = 3

# Fine bands — 5% spend, 0.1 ROAS. Usable at this resolution only because of
# the isotonic fit below.
SPEND_BANDS = [(i, i + 5) for i in range(0, 150, 5)] + [(150, 1e9)]
ROAS_BANDS = [(round(i * 0.1, 1), round((i + 1) * 0.1, 1)) for i in range(16)] + [(1.6, 1e9)]
L3_BANDS = [
    (-9e9, 0.5),    # declining / dead
    (0.5, 1.0),     # weak
    (1.0, 1.5),     # break-even zone
    (1.5, 2.0),     # trending up
    (2.0, 3.0),     # strong
    (3.0, 9e9),     # very strong
]


def band(v, bands):
    for lo, hi in bands:
        if lo <= v < hi:
            return (lo, hi)
    return None


def _pava(rates: list[float], wts: list[float]) -> list[float]:
    """Pool-adjacent-violators: nearest non-decreasing fit, weighted."""
    stack: list[list[float]] = []            # [weighted_sum, weight, n_pooled]
    for v, w in zip(rates, wts):
        cur = [v * w, w, 1]
        while stack and (stack[-1][0] / stack[-1][1]) > (cur[0] / cur[1]) + 1e-12:
            p = stack.pop()
            cur = [p[0] + cur[0], p[1] + cur[1], p[2] + cur[2]]
        stack.append(cur)
    out: list[float] = []
    for s, w, n in stack:
        out.extend([s / w] * int(n))
    return out


def load_camp_days(db_path: str, exclude_day: str | None = None) -> dict:
    """{(day, campaign_id): [(spend_pct, roas, last3h_roas), ...]} time-ordered.

    last3h is the INCREMENTAL ROAS over the trailing 3 hours — (Δrevenue/Δspend)
    against the newest snapshot at least 3h older. That is momentum, which the
    cumulative ROAS hides: a campaign at cumulative 0.8 climbing on 2.5 in the
    last three hours is a different animal from one flat at 0.8 all day.
    """
    con = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    try:
        rows = con.execute(
            "SELECT campaign_id, substr(hour_slot,1,10), CAST(substr(hour_slot,12,2) AS INTEGER), "
            "       COALESCE(daily_budget,0), COALESCE(spend,0), "
            "       COALESCE(revenue,0), COALESCE(roas,0) "
            "FROM campaign_hourly_snapshots ORDER BY campaign_id, hour_slot").fetchall()
    finally:
        con.close()

    raw: dict[tuple, list] = defaultdict(list)
    for cid, d, hr, budget, spend, rev, roas in rows:
        if exclude_day and d >= exclude_day:
            continue                      # today is incomplete — no EOD ROAS yet
        raw[(d, cid)].append((hr, budget, spend, rev, roas))

    out: dict[tuple, list] = {}
    for key, seq in raw.items():
        seq.sort()
        obs = []
        base = 0                          # two-pointer: newest snapshot >= 3h older
        for k, (hr, budget, spend, rev, roas) in enumerate(seq):
            while base + 1 <= k and seq[base + 1][0] <= hr - LAST3H_HOURS:
                base += 1
            b = seq[base] if seq[base][0] <= hr - LAST3H_HOURS else seq[0]
            d_sp, d_rev = spend - b[2], rev - b[3]
            last3h = (d_rev / d_sp) if d_sp > 0 else 0.0
            pct = (spend / budget * 100.0) if budget > 0 else 0.0
            obs.append((pct, roas, last3h))
        out[key] = obs
    return out


class SuccessLookup:
    def __init__(self, table2d, table3d, target, n_camp_days):
        self.table = table2d              # (sb, rb) -> rate 0-100 (isotonic-fitted)
        self.table3d = table3d            # (sb, rb, lb) -> rate 0-100
        self.target = target
        self.n_camp_days = n_camp_days

    def rate(self, spend_pct, roas):
        """2D rate. Returns (rate_pct, sample_n) — n is 0 when the cell is blank."""
        sb, rb = band(float(spend_pct or 0), SPEND_BANDS), band(float(roas or 0), ROAS_BANDS)
        v = self.table.get((sb, rb))
        return (v, 1) if v is not None else (None, 0)

    def rate_3d(self, spend_pct, roas, last3h):
        """3D rate with momentum. Returns (rate_pct, is_fallback).

        is_fallback=True means the 3D cell was too thin and this is the 2D
        answer — shown with a `*` so a momentum-blind number is never mistaken
        for a momentum-aware one.
        """
        sb = band(float(spend_pct or 0), SPEND_BANDS)
        rb = band(float(roas or 0), ROAS_BANDS)
        lb = band(float(last3h or 0), L3_BANDS)
        v = self.table3d.get((sb, rb, lb))
        if v is not None:
            return (v, False)
        v2 = self.table.get((sb, rb))
        return (v2, True) if v2 is not None else (None, False)


def build(db_path: str, target: float = TARGET_ROAS, exclude_day: str | None = None,
          camp_days: dict | None = None) -> SuccessLookup:
    """camp_days can be passed in to avoid re-reading the DB for a second target."""
    cd = camp_days if camp_days is not None else load_camp_days(db_path, exclude_day)
    if not cd:
        return SuccessLookup({}, {}, target, 0)

    agg2: dict = defaultdict(lambda: [0, 0])       # cell -> [n_days, n_success]
    agg3: dict = defaultdict(lambda: [0, 0])
    for seq in cd.values():
        if not seq:
            continue
        success = 1 if seq[-1][1] >= target else 0     # EOD-strict: last snapshot of the day
        s2, s3 = set(), set()
        for pct, ro, l3 in seq:
            sb, rb, lb = (band(pct, SPEND_BANDS), band(ro, ROAS_BANDS), band(l3, L3_BANDS))
            if sb and rb:
                s2.add((sb, rb))
                if lb:
                    s3.add((sb, rb, lb))
        for st in s2:
            agg2[st][0] += 1
            agg2[st][1] += success
        for st in s3:
            agg3[st][0] += 1
            agg3[st][1] += success

    # Isotonic fit per spend band, over ROAS ascending.
    by_sb: dict = defaultdict(list)
    for (sb, rb), (n, c) in agg2.items():
        by_sb[sb].append((rb, n, c))
    table2d = {}
    for sb, items in by_sb.items():
        items.sort(key=lambda x: x[0][0])
        fitted = _pava([c / n for (_rb, n, c) in items], [n for (_rb, n, _c) in items])
        for (rb, n, _c), fr in zip(items, fitted):
            if n >= MIN_SAMPLE:                # fit uses every bucket, publish only solid ones
                table2d[(sb, rb)] = round(100 * fr)

    table3d = {st: round(100 * c / n) for st, (n, c) in agg3.items() if n >= MIN_SAMPLE_3D}
    return SuccessLookup(table2d, table3d, target, len(cd))


def build_both(db_path: str, exclude_day: str | None = None):
    """Both targets off ONE pass over the snapshot table."""
    cd = load_camp_days(db_path, exclude_day)
    return (build(db_path, TARGET_ROAS, camp_days=cd),
            build(db_path, TARGET_ROAS_2, camp_days=cd))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default='state/camp_snapshots.db')
    ap.add_argument('--target', type=float, default=TARGET_ROAS)
    ap.add_argument('--show', action='store_true')
    args = ap.parse_args()

    lk = build(args.db, args.target)
    print(f"success lookup @ ROAS>={args.target}: {lk.n_camp_days} camp-days · "
          f"{len(lk.table)} 2D cells (>={MIN_SAMPLE} days, isotonic) · "
          f"{len(lk.table3d)} 3D cells (>={MIN_SAMPLE_3D} days, +last3h)")
    if not args.show:
        return
    rbs = sorted({rb for _sb, rb in lk.table}, key=lambda b: b[0])
    sbs = sorted({sb for sb, _rb in lk.table}, key=lambda b: b[0])
    print(f'\nrows = spend%, cols = ROAS, cell = % ending >= {args.target}\n')
    print('spend%'.ljust(8) + ''.join(f'{rb[0]:>6.1f}' for rb in rbs))
    for sb in sbs:
        line = f'{sb[0]:>4.0f}%  '
        for rb in rbs:
            v = lk.table.get((sb, rb))
            line += f'{"":>6}' if v is None else f'{str(v) + "%":>6}'
        print(line)


if __name__ == '__main__':
    main()
