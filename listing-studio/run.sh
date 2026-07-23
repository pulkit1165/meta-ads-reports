#!/bin/zsh
# Listing Studio — app + public tunnel
# local  : http://127.0.0.1:5757
# public : printed below and shown on the dashboard (…trycloudflare.com)
cd "$(dirname "$0")"
mkdir -p jobs

# stop stale copies
lsof -ti :5757 | xargs kill 2>/dev/null
pkill -f "cloudflared tunnel --url http://localhost:5757" 2>/dev/null
sleep 1

# app
nohup .venv/bin/python3 app.py > app.log 2>&1 &
echo "app started (app.log)"

# public tunnel (free Cloudflare quick tunnel; URL changes on each restart)
: > tunnel_url.txt
nohup cloudflared tunnel --url http://localhost:5757 > tunnel.log 2>&1 &
echo "tunnel starting…"
for i in {1..30}; do
  URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' tunnel.log | head -1)
  [ -n "$URL" ] && break
  sleep 1
done
if [ -n "$URL" ]; then
  echo "$URL" > tunnel_url.txt
  echo "PUBLIC URL: $URL"
else
  echo "tunnel did not come up — check tunnel.log (app still works on LAN)"
fi
