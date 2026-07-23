# Studd Muffyn Mobile App (Headless Shopify)

Premium black/white/gold mobile shopping app for **studdmuffyn.com**.
Shopify stays the single source of truth — products, prices, stock, coupons,
shipping and payments all come from Shopify. The app is a pure frontend.

## Run it on your phone (2 minutes)

1. Install **Expo Go** from the App Store / Play Store.
2. On this Mac:
   ```bash
   cd ~/meta-ads-reports/studd-muffyn-app/app
   npx expo start
   ```
3. Scan the QR code with your phone camera (iPhone) or Expo Go (Android).
   Phone and Mac must be on the same Wi-Fi.

To preview in a browser instead: `npx expo start --web` then open http://localhost:8081.

## What's inside

| Piece | Where | Notes |
|---|---|---|
| App source | `app/` | Expo (React Native + TypeScript), expo-router |
| Screens | `app/app/` | Home, Shop, Search, Wishlist, Profile, Collection, Product, Bag |
| Bundled catalog | `app/src/data/catalog.json` | 858 real products, 951 collections, real nav — crawled from the live site |
| Homepage config | `app/src/config/home.json` | **Every home section is JSON** — reorder/retitle/swap collections with zero code changes |
| Data layer | `app/src/api/shopify.ts` | Boots from bundled catalog, silently refreshes prices/stock/collection order live from Shopify public JSON |
| Crawler | `data/crawl.mjs`, `data/crawl_collections.mjs` | Re-crawl the site any time |
| Catalog builder | `data/build_catalog.mjs` | Rebuilds `catalog.json` from crawl output |

## How commerce works

- **Prices / stock / merchandised order**: bundled snapshot renders instantly, then
  each screen re-fetches live from `studdmuffyn.com/.../products.json` (no token needed).
- **Checkout**: cart hands off to real Shopify checkout via cart permalink
  (`/cart/VARIANT:QTY,...?discount=CODE`) — coupons, COD, shipping, payments,
  order emails all handled by Shopify exactly like the website.
- **Orders / tracking / account**: opens the real Shopify account pages in-app.

## Refresh the catalog (new products / prices)

```bash
cd ~/meta-ads-reports/studd-muffyn-app/data
node crawl.mjs               # all products (few minutes)
node crawl_collections.mjs   # all collections (30-45 min, resumable)
node build_catalog.mjs       # writes app/src/data/catalog.json
```

The app also self-refreshes live data at runtime, so rebuilding the bundle is
only needed occasionally (it sets the instant first paint).

## Merchandising — the website IS the dashboard

The app home screen auto-mirrors studdmuffyn.com:

1. Edit the website homepage in Shopify's theme customizer like you already do
   (banners, section order, featured collections, announcement bar).
2. `https://studd-muffyn-app.vercel.app/api/home-config` re-reads the site
   (cached 10 min) and converts it to the app's layout.
3. The app fetches that on every launch — changes appear in ~10 minutes.
   Offline / endpoint-down → the app falls back to its last good copy, then
   the bundled `app/src/config/home.json`.

Also automatic (direct from Shopify, instant): product pages, photos, prices,
stock, discounts, collection contents, new collections.

Needs a push (rare): app-wide colors/theme (`src/theme.ts`), Shop-tab
departments, new section *types* — one edit + `eas update` (over-the-air,
minutes, no store review).

A separate manual dashboard (app-specific overrides) is planned later; the
renderer is fully config-driven so it only needs to serve the same JSON.

## Product-page extras (reviews, pairs-well-with, detail sections)

The public Shopify JSON doesn't include Judge.me reviews, the "Pairs well
with" section, or the theme's detail tabs (Product Description / Key
Highlights / Hero Ingredients / Product Benefits / How to Use / FAQ). Those
are scraped from the website's own product pages with a real browser:

```bash
cd ~/meta-ads-reports/studd-muffyn-app/data
node scrape_extras.mjs        # resumable; ~40 min for the full catalog
bash deploy_web.sh            # deploys app + /extras/<handle>.json to Vercel
```

The app fetches `/extras/<handle>.json` per product and renders star rating,
review cards, the Pairs-well-with rail, and the extra accordion sections.
Re-run the scrape weekly (or after big catalog changes) to refresh reviews.

## Checkout: Shiprocket (Fastrr) + Shopify fallback

The app uses the same Shiprocket 1-click checkout as the website:

1. App cart → `POST /api/fastrr-checkout` (Vercel) → creates a Shiprocket
   checkout token (HMAC-signed server-side; keys never ship in the app).
2. App opens `checkout.html?token=…` → Shiprocket checkout UI (UPI/cards/COD,
   address book, OTP login). Success lands on `order-success.html`.
3. If Shiprocket keys aren't configured or their API is down, the app silently
   uses the Shopify cart-permalink checkout instead — checkout can never break.

**To activate:** get the custom-integration **API Key + API Secret** from your
Shiprocket Checkout account manager (the site already runs Fastrr, so the
account exists), then in Vercel → project `studd-muffyn-app` → Settings →
Environment Variables add `FASTRR_API_KEY` and `FASTRR_API_SECRET`, and
redeploy. No app change needed — it flips over automatically.

App orders arrive via Shiprocket exactly like the website's Fastrr orders
(custom attribute `source=studd_muffyn_app` tells them apart).

## Ship to stores (later)

Expo apps build for App Store / Play Store with EAS:
`npx eas build --platform all` (needs a free Expo account + store developer accounts).
