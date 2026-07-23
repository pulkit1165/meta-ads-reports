# Listing Studio

Upload raw product photos, reference/inspo images and label shots → get a complete
Shopify listing content package (title, description, SEO, tags, collections, variants,
metafields, FAQs) plus AI-generated listing images in the brand style.

## Start

```
./run.sh          # → http://<this-mac-ip>:5757  (all interfaces)
```

Team access: anyone on the same Wi-Fi opens `http://<mac-ip>:5757` (currently
10.172.99.117) and enters the team password once per browser. Password defaults to
`muffyn2026`; change with `STUDIO_PASSWORD=... ./run.sh`. Bind localhost-only with
`STUDIO_HOST=127.0.0.1`. Team guide:
https://claude.ai/code/artifact/1a2f8736-0410-4e40-827d-321b8b0f7ebc

## Flow

1. **+ New product** — name, category, price/MRP/SKU, notes, and three upload zones:
   - **Raw photos** — the actual product (used as the image-generation reference so
     generated images match the real product)
   - **References / inspo** — existing listings or competitor pages to match in tone/style
   - **Labels** — packaging shots; all text is read off them and becomes the factual
     basis for claims (ingredients, specs)
2. **1 · Generate content** (free) — a headless Claude run reads everything and writes
   `output/listing.json`. Review it on the page.
3. **2 · Generate images** (~2 Higgsfield credits per image, 6 images) — generates the
   category-appropriate 6-image sequence via Higgsfield, using the raw photos as
   reference. Per-image Regenerate buttons re-run single slots.

## Requirements

- Claude Code CLI logged in (uses `claude -p`, billed to the Max plan, no API key)
- Higgsfield connector attached to the claude.ai account (shows in `claude mcp list`)

## Storage

Everything lives under `jobs/<slug>/` — `inputs/{raw,refs,labels}/`,
`output/listing.json`, `output/images/`, `output/log.txt`. Gitignored.

Posting to Shopify (Part 2) will read `output/listing.json` + `output/images/` once
store Admin-API tokens exist.
