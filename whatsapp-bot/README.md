# whatsapp-bot — Team WhatsApp channel for Meta Ads reports

Receives WhatsApp messages from the team via Meta WhatsApp Cloud API, dispatches them to either:
- a deterministic slash command (runs a `scripts/*.py` directly, no LLM), or
- Claude Code in headless mode (`claude -p`) which uses Pulkit's Max subscription — no API tokens billed.

## Architecture

```
WhatsApp user (allowlisted team member)
    │
    ▼  POST /webhook   (HTTPS)
Meta WhatsApp Cloud API
    │
    ▼   (via Cloudflare Tunnel exposing localhost:8080)
server.py   (Flask)
    │
    ├── verify allowlist → drop if not allowed
    ├── /command?   → router.py dispatches to commands.py → runs scripts/*.py
    └── plain text? → claude_runner.py shells to `claude -p` (Max subscription)
    │
    ▼
wa_client.py — POST reply text to Meta Graph API
    │
    ▼
WhatsApp user receives reply
```

## Quick start (after Meta setup — see SETUP_META.md)

```bash
cd whatsapp-bot
cp .env.example .env       # fill in WA_*, ALLOWLIST, VERIFY_TOKEN
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
./run.sh                   # starts Flask on :8080
```

Then in another terminal run the Cloudflare Tunnel (see SETUP_TUNNEL.md) so Meta can reach `localhost:8080`.

## Files

| File | Purpose |
|---|---|
| `server.py` | Flask app: `/webhook` GET (Meta verification) + POST (message handler) |
| `router.py` | Routes incoming message to slash command or NL handler |
| `commands.py` | Slash command → script invocation registry |
| `claude_runner.py` | Invokes `claude -p` headless with report-system context |
| `wa_client.py` | Meta Graph API client (send text replies, mark read) |
| `.env.example` | Config template — copy to `.env` and fill in |
| `requirements.txt` | flask, requests, python-dotenv |
| `run.sh` | Start the server |
| `SETUP_META.md` | Step-by-step Meta Cloud API setup |
| `SETUP_TUNNEL.md` | Cloudflare Tunnel setup for local Mac |
