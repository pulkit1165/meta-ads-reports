# meta-ads-reports

Automated Meta Ads reporting — duplicated from the workspace at `~/.openclaw/workspace/` for migration to GitHub Actions.

## Status

**Phase 1 (current): duplication only.** Scripts are copied verbatim from the source. They still reference `/tmp/` for state and may not run as-is under GitHub Actions. The original scripts in `~/.openclaw/workspace/` remain the live production copies — **do not change behavior here without coordinating**.

**Phase 2 (next): refactor scripts** so state files go to a committed `state/` directory (persisted via a `state` branch) instead of `/tmp/`, and all config comes from env vars.

**Phase 3 (after that): GitHub Actions workflows** on the schedules described in the master doc.

## Layout

```
.
├── scripts/     # canonical Python scripts (copied from source)
├── docs/        # master documentation
├── .env.example # template — copy to .env locally
├── .gitignore
└── requirements.txt
```

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
