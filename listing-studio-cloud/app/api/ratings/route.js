import { NextResponse } from "next/server";
import { list, put } from "@vercel/blob";

const LOG = "ratings/log.json";
const LISTING = (slug) => `jobs/${slug}/output/listing.json`;

async function loadJson(prefix, fallback) {
  const { blobs } = await list({ prefix });
  if (!blobs.length) return fallback;
  const r = await fetch(blobs[0].url, { cache: "no-store" });
  return r.ok ? r.json() : fallback;
}
async function saveJson(pathname, data) {
  await put(pathname, JSON.stringify(data, null, 2), {
    access: "public", contentType: "application/json",
    addRandomSuffix: false, allowOverwrite: true, cacheControlMaxAge: 0,
  });
}

// Save a designer's 1–10 rating for a generated image.
// Stores it on the slot (so it shows on reload) and appends to a global
// ratings log so the best prompts can be reused/learned from.
export async function POST(req) {
  const b = await req.json();
  const slug = String(b.slug || "");
  const slot = b.slot;
  const rating = Math.max(1, Math.min(10, parseInt(b.rating, 10) || 0));
  if (!slug || slot == null || !rating) {
    return NextResponse.json({ error: "slug, slot and rating required" }, { status: 400 });
  }

  // 1) persist rating on the slot in listing.json
  const listing = await loadJson(LISTING(slug), null);
  if (listing) {
    const plan = (listing.image_plan || []).find((p) => p.slot === slot);
    if (plan) { plan.rating = rating; await saveJson(LISTING(slug), listing); }
  }

  // 2) append to the global ratings log (keep the most recent 800)
  const log = await loadJson(LOG, { items: [] });
  log.items.push({
    slug, slot, rating,
    productName: b.productName || "", category: b.category || "",
    prompt: b.prompt || "", theme: b.theme || "",
    overlay: b.overlay || "", textStyle: b.textStyle || "",
    imageUrl: b.imageUrl || "", ts: new Date().toISOString(),
  });
  if (log.items.length > 800) log.items = log.items.slice(-800);
  await saveJson(LOG, log);
  return NextResponse.json({ ok: true, rating });
}

// Return the top-rated prompts (rating ≥ 8) so the generator can offer them
// as proven starting points. Optional ?category= filters to the same category.
export async function GET(req) {
  const category = new URL(req.url).searchParams.get("category");
  const log = await loadJson(LOG, { items: [] });
  let items = log.items.filter((i) => i.rating >= 8 && i.prompt);
  if (category) {
    const same = items.filter((i) => (i.category || "").toLowerCase() === category.toLowerCase());
    if (same.length) items = same;
  }
  // de-dupe by prompt, highest rating first, cap 15
  const seen = new Set();
  const top = items.sort((a, b) => b.rating - a.rating).filter((i) => {
    const k = i.prompt.slice(0, 80);
    if (seen.has(k)) return false; seen.add(k); return true;
  }).slice(0, 15).map((i) => ({
    rating: i.rating, prompt: i.prompt, theme: i.theme,
    overlay: i.overlay, textStyle: i.textStyle, productName: i.productName,
  }));
  return NextResponse.json({ top });
}
