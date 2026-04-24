# meta-ads-reports

Automated Meta Ads reporting — duplicated from the workspace at `~/.openclaw/workspace/` for migration to GitHub Actions.

## Status

**Phase 1 ✅ duplication**: Scripts copied verbatim from `~/.openclaw/workspace/`. Original scripts there remain the live production copies.

**Phase 2 ✅ refactor for GitHub Actions**: Paths made portable — all hardcoded `/Users/.../workspace/` references replaced with repo-relative paths, `/tmp/` state files moved to `state/`, EC2 deploy in `auto_rebuild_dashboard.py` gated behind `ENABLE_EC2_DEPLOY=1` env var.

**Phase 3 (next): GitHub Actions workflows** on the schedules in the master doc, with a `state` branch to persist snapshot files between runs.

## Layout

```
.
├── scripts/     # canonical Python scripts (refactored, Phase 2)
├── state/       # snapshot/cache files (replaces /tmp/; committed to `state` branch in Phase 3)
├── out/         # generated artifacts (HTML dashboards)
├── docs/        # master documentation
├── .env.example # template — copy to .env locally
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
