import { getJob, jobOutputs } from "../../../../../lib/store";

// Amazon content pack — everything Seller Central's "Add a Product" form needs,
// derived from the generated listing. CSV of Field,Value rows for easy copy-paste
// (works today with zero Amazon API approval).

function esc(v) {
  const s = String(v ?? "");
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}
const strip = (html) => String(html || "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();

export async function GET(_req, { params }) {
  const { slug } = await params;
  const job = await getJob(slug);
  if (!job) return new Response("not found", { status: 404 });
  const { listing, images } = await jobOutputs(slug);
  if (!listing) return new Response("generate content first", { status: 400 });

  const pb = listing.page_blocks || {};
  // Amazon title: brand + product + key attribute, hard cap 200 chars
  const title = `${listing.vendor || "Studd Muffyn"} ${listing.title}`.slice(0, 200);

  // 5 bullets: the 4 USP detail points + care/benefit line, cleaned of emoji
  const deEmoji = (s) => String(s || "").replace(/[^\x20-\x7E₹|–-]/g, "").trim();
  const bullets = [
    ...(pb.detail_points || []).map(deEmoji),
    deEmoji((pb.product_benefits || "").split("\n").find(Boolean)),
    deEmoji(listing.metafields?.care_or_directions),
  ].filter(Boolean).slice(0, 5);

  // search terms: tags, no commas, ≤ 240 chars
  const seen = new Set();
  const terms = (listing.tags || [])
    .flatMap((t) => t.toLowerCase().split(/\s+/))
    .filter((w) => w && !seen.has(w) && seen.add(w))
    .join(" ").slice(0, 240);

  const v0 = listing.variants?.[0] || {};
  const imgs = images
    .map((im) => ({ url: im.url, slot: Number(im.pathname.match(/slot(\d+)/)?.[1] || 99) }))
    .sort((a, b) => a.slot - b.slot);

  const rows = [
    ["Field", "Value"],
    ["Product Name (Title, max 200)", title],
    ["Brand", listing.vendor || "Studd Muffyn"],
    ["Seller SKU", v0.sku || job.sku || ""],
    ["Your Price (INR)", v0.price ?? job.price ?? ""],
    ["MRP (INR)", v0.compare_at_price ?? job.mrp ?? ""],
    ["Bullet Point 1", bullets[0] || ""],
    ["Bullet Point 2", bullets[1] || ""],
    ["Bullet Point 3", bullets[2] || ""],
    ["Bullet Point 4", bullets[3] || ""],
    ["Bullet Point 5", bullets[4] || ""],
    ["Product Description", strip(listing.body_html)],
    ["Search Terms (no commas)", terms],
    ["Country of Origin", "India"],
    ["", ""],
    ["⚠ MAIN IMAGE RULE", "Amazon main image must be product-only on pure white, no text/logo/badges — use a clean photo (raw upload or the no-text hero). The ad-style images go as secondary images."],
    ...imgs.map((im, i) => [i === 0 ? "Image URLs (in order)" : "", im.url]),
    ...(listing.variants || []).slice(1).map((v, i) =>
      [`Variation ${i + 2} (${v.title})`, `SKU ${v.sku} · ₹${v.price} · MRP ₹${v.compare_at_price}`]),
  ];

  const csv = rows.map((r) => r.map(esc).join(",")).join("\n");
  return new Response("﻿" + csv, {
    headers: {
      "Content-Type": "text/csv; charset=utf-8",
      "Content-Disposition": `attachment; filename="${listing.handle || slug}-amazon.csv"`,
    },
  });
}
