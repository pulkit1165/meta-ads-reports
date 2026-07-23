import { list } from "@vercel/blob";
import { getJob, jobOutputs } from "../../../../../lib/store";

// Shopify product-import CSV — the no-API-token publishing path.
// Admin → Products → Import → upload this file → product lands as a Draft.
// Shopify fetches the images itself from the public Blob URLs.

const HEADERS = [
  "Handle", "Title", "Body (HTML)", "Vendor", "Type", "Tags", "Published",
  "Option1 Name", "Option1 Value",
  "Variant SKU", "Variant Grams", "Variant Inventory Tracker", "Variant Inventory Policy",
  "Variant Fulfillment Service", "Variant Price", "Variant Compare At Price",
  "Variant Requires Shipping", "Variant Taxable",
  "Image Src", "Image Position", "Image Alt Text",
  "SEO Title", "SEO Description", "Status",
];

function esc(v) {
  const s = String(v ?? "");
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

export async function GET(_req, { params }) {
  const { slug } = await params;
  const job = await getJob(slug);
  if (!job) return new Response("not found", { status: 404 });
  const { listing, images } = await jobOutputs(slug);
  if (!listing) return new Response("generate content first", { status: 400 });

  const handle = listing.handle || slug;
  const variants = listing.variants?.length
    ? listing.variants
    : [{ title: "Default Title", sku: job.sku, price: job.price, compare_at_price: job.mrp, grams: 0 }];

  // images sorted by slot number (slotN_ prefix)
  const imgs = images
    .map((im) => ({ url: im.url, slot: Number(im.pathname.match(/slot(\d+)/)?.[1] || 99) }))
    .sort((a, b) => a.slot - b.slot);
  const altBySlot = {};
  for (const p of listing.image_plan || []) altBySlot[p.slot] = p.alt || "";

  const rows = [HEADERS];
  const n = Math.max(variants.length, imgs.length, 1);
  for (let i = 0; i < n; i++) {
    const v = variants[i];
    const im = imgs[i];
    const first = i === 0;
    rows.push([
      handle,
      first ? listing.title : "",
      first ? listing.body_html : "",
      first ? listing.vendor || "Studd Muffyn" : "",
      first ? listing.product_type || "" : "",
      first ? (listing.tags || []).join(", ") : "",
      first ? "FALSE" : "",
      v ? "Size" : "",
      v ? v.title : "",
      v ? v.sku || "" : "",
      v ? v.grams || 0 : "",
      v ? "shopify" : "",
      v ? "deny" : "",
      v ? "manual" : "",
      v ? v.price ?? "" : "",
      v ? v.compare_at_price ?? "" : "",
      v ? "TRUE" : "",
      v ? "TRUE" : "",
      im ? im.url : "",
      im ? i + 1 : "",
      im ? altBySlot[im.slot] || "" : "",
      first ? listing.seo_title || "" : "",
      first ? listing.seo_description || "" : "",
      first ? "draft" : "",
    ]);
  }

  const csv = rows.map((r) => r.map(esc).join(",")).join("\n");
  return new Response(csv, {
    headers: {
      "Content-Type": "text/csv; charset=utf-8",
      "Content-Disposition": `attachment; filename="${handle}-shopify.csv"`,
    },
  });
}
