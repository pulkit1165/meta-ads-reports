# WhatsApp Channel — Team Guide

**Last updated:** 2026-05-13

WhatsApp is the team's request channel for Meta Ads reports. Message **Antriksh** on WhatsApp and you'll get reports back in chat.

---

## How it works

Send a slash command (predictable) **or** plain English (flexible). Antriksh runs the underlying script, formats the result, and replies.

- **Where:** DM Antriksh's number, or message in the *NTN Ads Execution Squad* WA group
- **Who can use it:** approved team members only (see allowlist below)
- **Latency:** most reports return in 30–90s; campaign tracker / category builds can take 2–3 min

---

## Slash commands

| Command | What you get |
|---|---|
| `/help` | This list |
| `/today` | Live monitor snapshot — active camps, spend, ROAS, closing risk |
| `/closing` | Current closing watchlist (latest 10 AM / 12 PM / 3 PM / 6 PM run) |
| `/categories [portal]` | Category × Sales/Retarget × ROAS. Portal = `sm` / `sml` / `nbp`. Omit for all three. |
| `/kpi` | Daily KPI summary (yesterday's portal totals, deltas vs prev day) |
| `/creatives [portal]` | Creative report top movers (1D/7D ROAS, spend %) |
| `/portal-roas` | On-demand version of the 9 AM Portal ROAS report |
| `/spend` | On-demand version of the 10 AM Ad Spend report |
| `/sale [date]` | Sale Report (Shopify orders × Meta spend). Default = yesterday. |
| `/gap` | Start Gap Budget protocol (Antriksh will ask for target + closed) |
| `/dashboard` | Link to the v2 dashboard |

## Natural-language requests (still work)

You can also just say:
- *"send me today's closing for SM"*
- *"reactivation budget"*
- *"frag01 KPI for last 7 days"*
- *"top creatives on NBP Skin"*

Antriksh has the full reporting-protocols memory loaded and will match what you mean. Use slash commands when you want fast and predictable; use plain text when the request is unusual or needs context.

---

## Hard rules (do not skip)

1. **No Meta changes without Pulkit's explicit approval.** Pauses, budget edits, new campaigns, creative changes — Antriksh will refuse and ping the *NTN Ads Execution Squad* group for approval.
2. **Don't share Antriksh's number outside the team.** It runs with production access.
3. **Group messages with `@Antriksh` only.** Without the mention, the bot won't respond in groups (avoids noise).

---

## Allowlist (approved senders)

| Name | Number | Scope |
|---|---|---|
| Pulkit | +91 95177 44959 | full (owner) |
| Kashish | +91 78891 66849 | ops, daily reports |
| Sam (Navdeep) | +91 82839 01380 | NTN, audience/overlap |
| Harpreet Sahota | +91 99880 90074 | Meta ads reports |
| Nia Khanna | +91 99158 68288 | Meta ads reports |
| Tajinder | +91 95925 73796 | all reports |
| Shipra | _number TBD_ | ads data |
| Jagdeep | _number TBD_ | ads data |

To add/remove, update the OpenClaw WhatsApp config (`channels.whatsapp.allowFrom`) and this table.

---

## When Antriksh doesn't respond

- Check gateway is up: `openclaw status` (should show "Gateway service ... running")
- Check WA session: `openclaw channels status --probe`
- Recent logs: `openclaw channels logs --channel whatsapp`
- Sender not allowlisted → message will be silently dropped
