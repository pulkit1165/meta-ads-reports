# Meta Message Template — for Scheduled Daily Reports

The bot can reply freely to anyone within a 24-hour window after they DM it. But for **bot-initiated** messages (like a 9 AM scheduled cron), Meta requires an **approved Message Template**.

## Why this matters

Without a template, the cron job can only deliver to recipients who DM'd the bot in the previous 24h. Each missed 24h window = silent delivery failure. Not acceptable for a daily report.

## Create the template (one-time, ~24h approval)

1. Open **WhatsApp Manager** → https://business.facebook.com/wa/manage/message-templates
2. Pick your WhatsApp Business Account `NTN Ads Bot` (ID `101654855980065`).
3. Click **Create template**.

### Template settings

- **Category**: **Utility** ✅ (free, fast approval — this is for service notifications)
  - Don't pick "Marketing" — slow approval, and rules limit when you can send.
  - Don't pick "Authentication" — only for OTPs.

- **Name**: `ntn_daily_meta_report`
  *(must match `DAILY_REPORT_TEMPLATE` in `.env` exactly)*

- **Languages**: **English (en)**

### Template content

**Header** (optional, skip for simplicity).

**Body** (1024 char limit):
```
📊 Daily Meta Ads Report — {{1}}

{{2}}

Reply with /help for the full command list, or just ask in plain English (e.g. "top creatives for SM Skin").
```

When you add `{{1}}` and `{{2}}`, Meta will ask for sample values for the review:
- `{{1}}` example: `14 May 2026, 09:00 IST`
- `{{2}}` example: `SM: 12 active camps · spend ₹45,000 · ROAS 7D 2.1x | SML: 8 · ₹22,000 · 1.8x | NBP: 15 · ₹61,000 · 2.4x`

**Footer** (optional, 60 char):
```
Antriksh · NTN Ads Bot
```

4. Click **Submit for review**.

## After approval

Meta sends an email when approved (usually 30 min – 6 hours for Utility category).

Then update `.env`:
```bash
DAILY_REPORT_TEMPLATE=ntn_daily_meta_report
```

…and reload the daily cron LaunchAgent. The cron will switch from free-form text to templated send automatically.

## Test the template before scheduling

```bash
cd whatsapp-bot
.venv/bin/python -c "
from scheduled_report import send_template
ok = send_template('919517744959', 'ntn_daily_meta_report',
                   ['14 May 2026, TEST', 'this is a test parameter'])
print('sent:', ok)
"
```

If `sent: True`, you'll receive the message in WhatsApp.

## If the template gets rejected

Common reasons:
- "Body looks like a marketing message" → reword to be transactional only ("Your report is ready", not "Check out…")
- Sample values too generic → use realistic example data
- Variable count mismatch → make sure body uses `{{1}}` and `{{2}}` exactly once each

Edit and resubmit — repeated submissions are fine.
