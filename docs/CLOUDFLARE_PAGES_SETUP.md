# Cloudflare Pages — Live Dashboard Setup

End state: a permanent URL like `https://meta-ads-reports.pages.dev/` that auto-refreshes after every workflow run, serving the NTN dashboard at the root and the standalone today-live dashboard at `/today_live.html`. Optional email-gated access via Cloudflare Access (free).

The workflows are already wired up — they just stay dormant until you complete steps 1-5 below. Once you set the GitHub variable, the next workflow run deploys.

---

## Step 1 — Sign up for Cloudflare (if you don't have an account)

https://dash.cloudflare.com/sign-up — free, no credit card.

## Step 2 — Create the Pages project

1. Open https://dash.cloudflare.com → left sidebar → **Workers & Pages**
2. Click **Create application** → tab **Pages** → click **Direct Upload**
3. Project name: **`meta-ads-reports`** (or whatever — this becomes part of the URL)
4. Click **Create project**
5. Skip the "upload assets" step on the next page — we'll deploy via GitHub Actions instead. Just leave the project sitting empty.

Your URL will be `https://<project-name>.pages.dev/`.

## Step 3 — Get your Cloudflare credentials

You need two values:

**A. Account ID** — visible on the right sidebar of the Workers & Pages page you just left, OR in any project's overview page.

**B. API Token** — restricted to just Pages deploys:

1. Top-right of any Cloudflare page → **My Profile** → tab **API Tokens**
2. Click **Create Token**
3. Find the **Edit Cloudflare Workers** template OR scroll to **Custom token** and create one with these permissions:
   - **Account → Cloudflare Pages → Edit**
4. Account Resources: **Include → Specific account → <your account>**
5. Click **Continue to summary** → **Create Token**
6. **Copy the token now** — Cloudflare won't show it again. Paste it somewhere temporary.

## Step 4 — Add 2 GitHub secrets + 1 variable

Open https://github.com/pulkit1165/meta-ads-reports/settings/secrets/actions

**Secrets** (sensitive — contents are hidden after save):
1. Click **New repository secret**:
   - Name: `CLOUDFLARE_API_TOKEN`
   - Value: paste the token from Step 3B
2. Click **New repository secret** again:
   - Name: `CLOUDFLARE_ACCOUNT_ID`
   - Value: paste the Account ID from Step 3A

Then switch to the **Variables** tab on the same page (https://github.com/pulkit1165/meta-ads-reports/settings/variables/actions):

3. Click **New repository variable**:
   - Name: `CLOUDFLARE_PAGES_PROJECT`
   - Value: the project name from Step 2 (e.g. `meta-ads-reports`)

The variable is what activates the deploy steps. As long as it's empty, deploys are silently skipped — that's the safety switch.

## Step 5 — Trigger a deploy and visit the URL

Trigger a fresh workflow run:

- https://github.com/pulkit1165/meta-ads-reports/actions/workflows/today-live.yml → **Run workflow** → green button

Wait ~3 min for it to finish. The new "Deploy to Cloudflare Pages" step should run successfully.

Then open: `https://<project-name>.pages.dev/`

That serves `ntn_filtered.html` at the root. The standalone today-live dashboard is at `/today_live.html`. Both auto-refresh hourly going forward.

## Optional — gate access by email (Cloudflare Access)

By default the Pages URL is public. To restrict to specific email addresses (free with the Cloudflare Zero Trust plan):

1. Cloudflare dashboard → left sidebar → **Zero Trust** (sign up if first time — free for up to 50 users)
2. **Access** → **Applications** → **Add an application** → **Self-hosted**
3. **Application name**: `Meta Ads Reports`
4. **Application domain**: pick **`pages.dev`** subdomain → **`<project-name>.pages.dev`**
5. **Identity providers**: keep "One-time PIN" (default — sends a code to email)
6. **Add policy** → name: "Allowed users" → **Action: Allow** → **Include → Emails → `pulkitsharma1165@gmail.com`** (and any teammates)
7. Save → done

Now anyone visiting `<project-name>.pages.dev` gets prompted for an email; Cloudflare emails them a 6-digit code; only addresses on your allow-list get in.

## Optional — custom domain

Want `dashboards.studd-muffyn.in` instead of `*.pages.dev`?

1. In the Pages project → **Custom domains** → **Set up a custom domain**
2. Type your domain (e.g. `dashboards.studd-muffyn.in`)
3. Cloudflare will tell you the DNS CNAME to add at your registrar (or auto-add it if the domain is already on Cloudflare)
4. Wait ~minute for DNS propagation

If your domain isn't on Cloudflare yet, you can [add it as a free site](https://developers.cloudflare.com/dns/zone-setups/full-setup/setup/) first.

---

## Troubleshooting

| Error in the deploy step | Likely cause | Fix |
|---|---|---|
| `Project not found` | `CLOUDFLARE_PAGES_PROJECT` value doesn't match the project name in Cloudflare | Check the spelling; must match exactly |
| `Authentication error: code: 10000` | API token wrong / lacks `Pages:Edit` scope | Re-issue the token following Step 3B exactly |
| `Account access denied` | Wrong Account ID | Re-copy Account ID from Cloudflare sidebar |
| Step is silently skipped | `CLOUDFLARE_PAGES_PROJECT` variable not set | Set it in Settings → Variables → Actions |
