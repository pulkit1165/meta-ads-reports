# Meta WhatsApp Cloud API Setup

Step-by-step to get a working WhatsApp number that hits your bot. Estimated time: **30–60 minutes**, free.

## What you'll end up with

- A **test WhatsApp business number** issued by Meta (e.g. `+1 555 …`) that you can message immediately
- An **access token** and **phone number ID** for the bot to use
- A **webhook** pointed at your Mac via Cloudflare Tunnel
- 1000 free conversations/month forever (Meta's tier)

Later you can graduate to a *real* business number (your own SIM or virtual), but the test number is enough to validate everything.

---

## Step 1 — Create a Meta App

1. Go to **https://developers.facebook.com/apps** → sign in with your personal Facebook account (or your business account if you have one).
2. Click **Create App** → choose **"Business"** as the use case → Next.
3. App name: `NTN Ads Bot` (or anything). Email: your email. Click **Create app**.
4. You're now in the App Dashboard.

## Step 2 — Add the WhatsApp product

1. In the app dashboard, scroll to **"Add products to your app"** → find **WhatsApp** → click **Set up**.
2. Meta will ask to **link a Business Account** — pick an existing Meta Business Manager, or create one (one-click, no paperwork for the test setup).
3. You'll land on the **API Setup** page. Save these values somewhere — you'll need them for `.env`:
   - **Phone number ID** (under "From"): a long number like `123456789012345`
   - **WhatsApp Business Account ID**: a different long number
   - **Temporary access token** (top of page): starts with `EAA…`, valid 24h. We'll upgrade to a permanent one later.

## Step 3 — Add YOUR number as a test recipient

Test numbers can only message phone numbers that are pre-allowed.

1. On the API Setup page → **"To"** dropdown → **Add phone number** → enter your number `+91 9517744959`.
2. Meta sends a code via WhatsApp to that number → enter it → done.
3. You can add up to 4 more numbers later (the rest of your team). For now just you, to test.

## Step 4 — Set up the webhook (uses Cloudflare Tunnel — see SETUP_TUNNEL.md first)

1. Get your public tunnel URL running — should look like `https://something.trycloudflare.com` or `https://wa.yourdomain.com`.
2. In the app dashboard → **WhatsApp** → **Configuration** → **Webhook** → **Edit**.
3. **Callback URL**: `https://<your-tunnel>/webhook`
4. **Verify token**: paste the same string you put in `.env` as `WA_VERIFY_TOKEN` (any random string, e.g. `ntn_wa_v1_xyz789`).
5. Click **Verify and save**. Meta hits your `GET /webhook` — you should see "webhook verified" in the bot log.
6. Below that, **Webhook fields** → **Manage** → enable **`messages`** field.

## Step 5 — Generate a permanent access token

The temporary 24h token will expire. Get a permanent one:

1. **Business Settings** (top-right gear) → **Users** → **System Users** → **Add** → name it `ntn-wa-bot`, role **Admin** → Create.
2. Click the system user → **Generate new token** → select your app → check **`whatsapp_business_messaging`** and **`whatsapp_business_management`** → **Generate**.
3. Copy the token (long, starts with `EAA…`) — this is what goes in `.env` as `WA_ACCESS_TOKEN`. **Permanent, won't expire.**

## Step 6 — Fill in `.env` and start the bot

```bash
cd whatsapp-bot
cp .env.example .env
# Edit .env — paste your WA_PHONE_NUMBER_ID, WA_ACCESS_TOKEN, WA_VERIFY_TOKEN
./run.sh
```

## Step 7 — Test

From your phone (the one you added in Step 3), WhatsApp the Meta test number with `/ping`.

Expected: you receive `pong 🦞` within 5 seconds.

Then try `/help`, `/today`, etc.

---

## Going to production later

- **Get a real business number** (Meta or a verified business display name) — in API Setup → "Add phone number" → choose to migrate one of your existing numbers, or buy a new virtual one. Free tier still applies.
- **Move bot from Mac to EC2** — same code, run on `13.126.250.175` instead. Update Cloudflare Tunnel target.
- **Increase allowlist** — add the remaining team members in `.env` (`ALLOWLIST=` comma-separated E.164 without +).
