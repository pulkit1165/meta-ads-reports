# Publishing the new app as an UPDATE to the existing store listings

You already have live apps on both stores, so this is a version update — no new
accounts, no new listings, no D-U-N-S. The new build just has to ship under the
same app identity, from the same developer accounts, with a higher version.

## Your existing apps (found on the stores)

| | Android | iOS |
|---|---|---|
| Listing | play.google.com/store/apps/details?id=com.studdmuffinn.shopifyapp | apps.apple.com/in/app/studd-muffyn/id1597059141 |
| App identity | package `com.studdmuffinn.shopifyapp` (note the double-n spelling) | App Store ID `1597059141`; bundle ID to confirm in App Store Connect |
| Current version | 4.121 (updated 20 Jan 2026) | check in App Store Connect |
| Developer contact on listing | care.studdmuffyn@gmail.com | — |

The new app is already configured to match: `app.json` uses package/bundle
`com.studdmuffinn.shopifyapp`, version `5.0.0`, Android versionCode `50000`
(safely above 4.121's internal code). `eas.json` is set to submit to Apple app
`1597059141`.

## What only you can provide (the 3 keys to the castle)

1. **Google Play Console login** — the account that owns the Studd Muffyn app.
   In Play Console open the app → **Test and release → Setup → App signing**:
   - If it says **"Play App Signing enabled"** (almost certain): we sign uploads
     with an *upload key*. If the old upload key was held by whoever built the
     current app (it's a Shopify app-builder app), we simply request an
     **upload key reset** from that same page — Google approves in ~2 days and
     our new key becomes valid. No user is affected.
2. **App Store Connect access** (appstoreconnect.apple.com) for the Apple
   account that owns app 1597059141 — Admin or App Manager role. From there I
   also confirm the exact iOS bundle ID (visible under App Information) and
   correct `app.json` if it differs from the Android package.
3. **An Expo account** (free, expo.dev) — EAS builds the store binaries in the
   cloud and manages iOS certificates automatically (fresh certificates are
   fine for updates; only Android upload keys need the step above).

**Also:** if the current app was made by a Shopify app-builder platform
(Vajro / Plobal / Appmaker etc.), cancel that subscription only *after* the new
version is live, and don't let the platform push any update in between.

## Then the actual release (I run these with you)

```bash
npm install -g eas-cli && eas login
cd ~/meta-ads-reports/studd-muffyn-app/app
eas credentials            # one-time: set up Android upload key + iOS certs
eas build --platform all   # cloud build, ~30 min
eas submit --platform android
eas submit --platform ios
```

Then in each console:
- **Play Console**: the build appears under Production → create release →
  release notes ("Completely redesigned app…") → roll out. Review: usually
  hours to ~2 days for an established app.
- **App Store Connect**: new version 5.0.0 → attach build → release notes →
  Submit for review. Review: typically 1–2 days. Choose "automatically release
  after approval".

## Timeline

- With console access + upload key in hand: **2–4 days** to both stores.
- If we need the Google upload-key reset: **add ~2 days**.

## After launch

- Catalog, prices, banners, homepage layout: update live with **no store
  review** (Shopify data + JSON config, plus `eas update` for over-the-air JS changes).
- The announcement bar advertises 30% off the first app order — make sure that
  discount code exists in Shopify before rollout.
- Existing users get the new app as a normal update; carts/wishlists from the
  old app don't migrate (different tech), which is standard for replatformed apps.
