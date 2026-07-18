# Camp closing + hourly blended ROAS

Two automated reports, both driven by `.github/workflows/hourly-reports.yml`
(twice an hour, `camp-live` concurrency group).

## Report 1 — hourly blended ROAS

`scripts/v2/hourly_blended_report.py` → sheet tab **Hourly Blended ROAS** + WhatsApp.

| Column | Meaning |
|---|---|
| Portal | SM / SML / NBP, plus an `ALL` row per hour |
| Shopify Sale ₹ | real Shopify revenue created that hour, cancellations excluded |
| Ad Spend ₹ | Meta spend incurred that hour |
| Product Suggestions | distinct products live on ads that hour |
| Blended ROAS | Shopify sale ÷ ad spend |
| Δ Products | change vs the same portal's previous hour — the "5 got closed" number |

**Why blended and not per-campaign.** Only ~24% of Shopify orders carry a
`utm_campaign`. Attributing Shopify revenue to individual campaigns would drop
three quarters of real revenue and make every campaign look broken. At portal
grain every order counts. Per-campaign numbers stay on Meta pixel, on the
existing live-ROAS dashboard — the two are not comparable, and blended is lower
and truer.

**`ALL` products is a distinct union, not a sum.** The same product often runs
on SM and SML at once.

**"no snapshot" cells.** GitHub's cron skips ticks, so an hour can have Shopify
sales but no Meta snapshot. Those hours are labelled, not zero-filled — a zero
would read as "every product got closed this hour", which is precisely the
signal the report exists to convey.

### Known blocker: SM and NBP Shopify ingest is dead

`shopify_orders` has had **no SM or NBP rows since 18 June 2026**; SML keeps
working. `ingest_shopify` logs `success` every 10 minutes regardless, because
`fetch_orders()` prints a warning and returns `[]` when credentials fail
(`scripts/v2/ingest_shopify.py:80`). All six Shopify secrets in GitHub date from
1 May, and the stores were re-tokenised 8–9 July — those new tokens were never
written back to the secrets.

Until `SHOPIFY_ACCESS_TOKEN` and `SHOPIFY_ACCESS_TOKEN_NBP` are refreshed,
**blended ROAS for SM and NBP reads 0.00** and only SML is meaningful.

## Report 2 — camp closing

`scripts/v2/camp_closing.py` → sheet tab **Camp Closing** + WhatsApp, and the
auto-pause actuator.

Verdicts: `PAUSE` (kill matrix hit) · `REVIEW` (matrix says keep, history says
almost nothing recovers — never auto-paused) · `WATCH` · `SCALE` · `OK`.

Kill matrix, spend% of daily budget × ROAS:

| spend ≥ | pause if ROAS ≤ |
|---|---|
| 50% | 0.5 |
| 40% | 0.4 |
| 35% | 0.3 |
| 30% | 0.2 |
| 25% | 0.1 |

Rails, all of which must pass before anything is paused: portal whitelist
(SM/SML/NBP), 06:30–23:45 IST window, once per campaign per IST day, status
ACTIVE, and a circuit breaker at 25 pauses per run. Every decision is archived
to `camp_closing_log` in `camp_snapshots.db`.

### Success rate at 1.6 and 2.1

`scripts/v2/success_lookup.py` answers: *given a campaign is at X% of budget
with ROAS Y, how often did campaigns in that state finish the day at ≥ target?*
Built from ~2,700 completed camp-days at 5%-spend × 0.1-ROAS resolution. Each
camp-day counts at most once per cell so a lingering campaign can't dominate,
and success is EOD-strict (the day's last snapshot, not any peak).

Three things ported from the EC2 `simple_camp_db.py`, which had solved problems
this repo hadn't:

1. **Isotonic smoothing (PAVA).** Raw cell rates are noisy — a lower-ROAS bucket
   could show a *higher* recovery rate than a higher-ROAS one, which is
   impossible. The fit forces the rate non-decreasing as ROAS rises within each
   spend band, pooling violating neighbours weighted by sample count. This is
   what makes 5%×0.1 bands usable at all; without it the honest resolution is
   about 10%×0.2 and half the grid reads as noise.
2. **3D lookup with momentum.** Adds the last-3h incremental ROAS
   (Δrevenue/Δspend over the trailing 3 hours) as a third axis. Two campaigns at
   the same spend%/ROAS but different momentum have very different odds. 615
   populated 3D cells. A value shown as `42%*` means the 3D cell was too thin
   and it fell back to the momentum-blind 2D number.
3. **Second target at 2.1** alongside 1.6, so "will it clear break-even" and
   "is it worth scaling" are separate questions.

Also ported: `new_or_reactive` — a campaign whose first-ever spending day is
today is `new`; one that spent before and is live again is `reactivated`. They
carry different risk (one has learning history, one doesn't). Campaigns first
seen on the DB's own earliest day are left blank rather than mislabelled `new`.

Run `python3 scripts/v2/success_lookup.py --db state/camp_snapshots.db --show`
for the full grid. It is a clean, monotonic decision surface — at ≥50% spend
with ROAS ≤0.6, 0–3% of campaigns recover; at ROAS ≥1.6, 92–99% do.

## Why auto-pause ships DISARMED

The workflow defaults to `--dry-run`. Arming it is a one-line change: set
repository variable `AUTOPAUSE_LIVE=1`. It is not armed by default because a
22-day backtest says the matrix does not make money.

For each decision hour, every campaign the matrix would have paused was tracked
to end of day: how much it went on to spend, and how much revenue it went on to
earn.

| decide at | camp-days | spend avoided | revenue given up | net | ROAS of abandoned spend |
|---|---|---|---|---|---|
| 08:00 | 59 | ₹93,442 | ₹80,763 | **+₹12,680** | 0.86 |
| 10:00 | 123 | ₹172,001 | ₹185,494 | −₹13,493 | 1.08 |
| 12:00 | 165 | ₹164,594 | ₹188,578 | −₹23,984 | 1.15 |
| 14:00 | 130 | ₹116,481 | ₹123,660 | −₹7,180 | 1.06 |
| 16:00 | 103 | ₹71,197 | ₹59,774 | **+₹11,422** | 0.84 |
| 18:00 | 69 | ₹21,844 | ₹23,574 | −₹1,730 | 1.08 |
| 20:00 | 54 | ₹7,759 | ₹11,514 | −₹3,755 | 1.48 |

Over 22 days the net oscillates around zero. The spend being killed runs at
roughly break-even, not at the ROAS 0.1–0.5 the campaigns showed at the moment
of the decision — campaigns that look dead in the morning substantially recover.
That matches the standing rule that partial-day ROAS is unreliable.

A widened rule keyed on the success lookup (pause when historical recovery ≤5%
and spend ≥30%) tested **worse**: 56 camp-days, ₹113,947 spend avoided against
₹193,565 revenue given up — net −₹79,618, abandoning spend that was running at
ROAS 1.70.

**Caveat, stated plainly:** post-pause revenue includes conversions attributed to
impressions already served before the pause, so "revenue given up" is somewhat
overstated — pausing would not have destroyed all of it. That biases the table
against auto-pause. But the margin is nowhere near large enough to justify
unsupervised pausing of live campaigns, and the two hours that do show a profit
(+₹12.7k and +₹11.4k over 22 days ≈ ₹550/day) are well inside the noise.

### Suggested path to arming it

1. Leave it dry-run for a week and read the `Camp Closing` tab daily. The
   `Action Taken` column shows exactly what it would have done.
2. If the WOULD-PAUSE list matches your own manual closing decisions, arm it.
3. Re-run the backtest once there is more history:
   `python3 scripts/v2/camp_closing.py --db state/camp_snapshots.db --day <d> --as-of '<d> 10:00' --dry-run --no-sheet`

To un-pause anything the engine closed:
`SELECT campaign_id, campaign_name FROM camp_closing_log WHERE day='<today>' AND paused=1;`

## Setup still needed

- **Shopify tokens** — refresh `SHOPIFY_ACCESS_TOKEN` and
  `SHOPIFY_ACCESS_TOKEN_NBP` (see blocker above). Without this Report 1 only
  works for SML.
- **WhatsApp secrets** — add `WA_ACCESS_TOKEN`, `WA_PHONE_NUMBER_ID`,
  `WA_REPORT_RECIPIENTS` (comma-separated, E.164 without `+`). Copy from
  `whatsapp-bot/.env`. Both reports skip the send silently until these exist.
  Outside a 24h conversation window Meta requires an approved template — set
  `WA_REPORT_TEMPLATE` to its name, or expect free-form sends to fail.
- **`ads_management` scope** — auto-pause needs it on `META_ACCESS_TOKEN`. The
  current token is used for reads only; if it lacks the scope, pause calls fail
  and are logged to `camp_closing_log.error` rather than failing the run.
