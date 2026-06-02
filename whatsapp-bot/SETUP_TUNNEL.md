# Cloudflare Tunnel — Expose Mac to Meta's webhook

Meta needs a public HTTPS URL to send webhook events to. Your Mac is behind NAT, so we use **Cloudflare Tunnel** (free, persistent, already part of your stack).

## Option A — Quick tunnel (no domain needed, random URL, restarts each time)

Good for testing. URL looks like `https://random-words.trycloudflare.com` and changes every time you restart.

```bash
brew install cloudflared       # one-time
cloudflared tunnel --url http://localhost:8080
```

Cloudflared prints a URL — paste `<url>/webhook` into Meta's webhook config.

**Downside:** URL changes on restart → you'd have to re-paste in Meta. Fine for the first test, painful for prod.

## Option B — Named tunnel under your own domain (recommended)

Stable URL like `https://wa.desistuddmuffyn.in` that survives restarts.

You already use Cloudflare for `desistuddmuffyn.in` (per ANTRIKSH memory), so:

```bash
brew install cloudflared
cloudflared tunnel login                          # opens browser → pick desistuddmuffyn.in zone
cloudflared tunnel create ntn-wa-bot              # creates tunnel, prints UUID + saves creds
cloudflared tunnel route dns ntn-wa-bot wa.desistuddmuffyn.in
```

Create `~/.cloudflared/config.yml`:

```yaml
tunnel: ntn-wa-bot
credentials-file: /Users/pulkitsharma/.cloudflared/<UUID>.json
ingress:
  - hostname: wa.desistuddmuffyn.in
    service: http://localhost:8080
  - service: http_status:404
```

Start it:

```bash
cloudflared tunnel run ntn-wa-bot
```

Or install it as a launch agent so it runs always:

```bash
sudo cloudflared service install
```

Now `https://wa.desistuddmuffyn.in/webhook` always points at your local bot.

## Verification

With both the bot and the tunnel running:

```bash
curl -s https://wa.desistuddmuffyn.in/healthz
# → {"ok": true, "allowlist_size": 1}
```

If you see that JSON, you're ready for Meta's webhook verification step.

## When you move to EC2

Drop the tunnel entirely — EC2 already has a public IP. Set up nginx or run gunicorn directly on a public port, attach a Cloudflare proxy on `wa.desistuddmuffyn.in` pointing at the EC2 IP, done.
