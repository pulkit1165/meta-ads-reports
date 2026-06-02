#!/usr/bin/env bash
# Start the WhatsApp bot. Creates venv on first run.
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  echo "❌ No .env — copy .env.example to .env and fill in WA_* + ALLOWLIST first."
  exit 1
fi

if [[ ! -d .venv ]]; then
  echo "📦 Creating virtualenv…"
  python3 -m venv .venv
  ./.venv/bin/pip install --upgrade pip
  ./.venv/bin/pip install -r requirements.txt
fi

# Prevent Mac from sleeping while the bot is running (so webhooks keep working).
# `caffeinate -i` keeps the system awake; remove if you don't want this behavior.
exec caffeinate -i ./.venv/bin/python server.py
