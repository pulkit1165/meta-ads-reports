import { NextResponse } from "next/server";
import { list, put } from "@vercel/blob";

async function loadListing(slug) {
  const { blobs } = await list({ prefix: `jobs/${slug}/output/listing.json` });
  if (!blobs.length) return null;
  const r = await fetch(blobs[0].url, { cache: "no-store" });
  return r.ok ? r.json() : null;
}

async function saveListing(slug, listing) {
  await put(`jobs/${slug}/output/listing.json`, JSON.stringify(listing, null, 2), {
    access: "public",
    contentType: "application/json",
    addRandomSuffix: false,
    allowOverwrite: true,
    cacheControlMaxAge: 0,
  });
}

// Edit the image plan: update a slot's prompt, or add a new slot.
export async function POST(req, { params }) {
  const { slug } = await params;
  const listing = await loadListing(slug);
  if (!listing) return NextResponse.json({ error: "generate content first" }, { status: 400 });
  const b = await req.json();
  listing.image_plan = listing.image_plan || [];

  if (b.action === "update") {
    const plan = listing.image_plan.find((p) => p.slot === b.slot);
    if (!plan) return NextResponse.json({ error: "slot not found" }, { status: 404 });
    if (typeof b.prompt === "string") plan.prompt = b.prompt;
    if (typeof b.theme === "string") plan.theme = b.theme;
    if (typeof b.overlay_text === "string") plan.overlay_text = b.overlay_text;
    if (typeof b.text_style === "string") plan.text_style = b.text_style;
    if (typeof b.role === "string" && b.role.trim()) plan.role = b.role.trim();
    if (typeof b.alt === "string" && b.alt.trim()) plan.alt = b.alt.trim();
    if (Array.isArray(b.addRefs)) {
      plan.refs = plan.refs || [];
      for (const r of b.addRefs) {
        if (r?.url && ["raw", "inspo"].includes(r.kind)) {
          plan.refs.push({ url: r.url, name: r.name || "", kind: r.kind });
        }
      }
    }
    if (Array.isArray(b.removeRefUrls)) {
      plan.refs = (plan.refs || []).filter((r) => !b.removeRefUrls.includes(r.url));
    }
  } else if (b.action === "add") {
    const slot = Math.max(0, ...listing.image_plan.map((p) => p.slot)) + 1;
    const role = (b.role || `extra image ${slot}`).trim();
    const roleSlug = role.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
    listing.image_plan.push({
      slot,
      role,
      filename: `${listing.handle || slug}-${roleSlug || slot}.jpg`,
      alt: b.alt?.trim() || `${listing.title || slug} — ${role}`,
      theme: b.theme || "",
      overlay_text: b.overlay_text || "",
      text_style: b.text_style || "",
      prompt: b.prompt || "",
    });
  } else {
    return NextResponse.json({ error: "bad action" }, { status: 400 });
  }

  await saveListing(slug, listing);
  return NextResponse.json({ ok: true, image_plan: listing.image_plan });
}
