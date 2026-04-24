# meta-ads-reports

Automated Meta Ads reporting — duplicated from the workspace at `~/.openclaw/workspace/` for migration to GitHub Actions.

## Status

**Phase 1 ✅ duplication**: Scripts copied verbatim from `~/.openclaw/workspace/`. Original scripts there remain the live production copies.

**Phase 2 ✅ refactor for GitHub Actions**: Paths made portable — all hardcoded `/Users/.../workspace/` references replaced with repo-relative paths, `/tmp/` state files moved to `state/`, EC2 deploy in `auto_rebuild_dashboard.py` gated behind `ENABLE_EC2_DEPLOY=1` env var.

**Phase 3 ✅ GitHub Actions workflows**: One workflow per schedule (see [SECRETS.md](SECRETS.md) for the table). Snapshot files persist between runs via an orphan `state` branch managed by a small composite action (`.github/actions/state-sync`). **Before any workflow will work, you must add two secrets** — see [SECRETS.md](SECRETS.md).

## Layout

```
.
├── scripts/               # canonical Python scripts (Phase 2-refactored)
├── state/                 # snapshot/cache files (persisted via `state` branch)
├── out/                   # generated artifacts (HTML dashboards)
├── config/
│   └── accounts.env       # 15 ad account IDs — loaded into $GITHUB_ENV
├── docs/                  # master documentation
├── .github/
│   ├── actions/
│   │   └── state-sync/    # composite action — pull/push `state` branch
│   └── workflows/
│       ├── daily.yml              # 04:30 IST — full pipeline
│       ├── closing-watchlist.yml  # 10:00 / 12:00 / 15:00 / 18:00 IST
│       ├── live-monitor.yml       # 3-hourly at :30 IST
│       └── live-closing-alert.yml # 13:00 IST
├── SECRETS.md             # instructions for adding the 2 required GitHub secrets
├── .env.example
├── .gitignore
└── requirements.txt
```

## Phase 2 path conventions

Every script now resolves paths relative to the repo root:

```python
_REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR  = _REPO_ROOT / 'state'    # override: META_REPORTS_STATE_DIR
OUT_DIR    = _REPO_ROOT / 'out'      # override: META_REPORTS_OUT_DIR
SA_FILE    = _REPO_ROOT / 'google-service-account.json'   # override: GOOGLE_SERVICE_ACCOUNT_FILE
load_dotenv(_REPO_ROOT / '.env')
```

Environment variables (from the process environment, e.g. GitHub Actions secrets) take precedence over `.env` file values where both are present.

## Full reference

See [docs/META_REPORTING_MASTER_DOC.md](docs/META_REPORTING_MASTER_DOC.md) for:
- All 7 reports, their schedules, and business logic
- All 15 ad accounts and 3 Shopify portals
- ROAS thresholds, budget rules, closing logic
- Google Sheets IDs and tab naming conventions

## Local setup (for testing before Phase 3)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in real values
cp ~/.openclaw/workspace/google-service-account.json .
python3 scripts/campaign_tracker_builder.py --help
```
