import { NextResponse } from "next/server";
import { allJobs, getJob, saveJob } from "../../../lib/store";

function slugify(name) {
  return (
    name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") ||
    `product-${Date.now()}`
  );
}

export async function GET() {
  return NextResponse.json(await allJobs());
}

export async function POST(req) {
  const b = await req.json();
  if (!b.name?.trim()) return NextResponse.json({ error: "name required" }, { status: 400 });
  let slug = slugify(b.name);
  if (await getJob(slug)) slug = `${slug}-${Date.now() % 10000}`;
  const job = {
    slug,
    name: b.name.trim(),
    category: b.category || "Other",
    vendor: b.vendor || "Studd Muffyn",
    price: b.price || "",
    mrp: b.mrp || "",
    sku: b.sku || "",
    variants: b.variants || "",
    jewellery_finish: b.jewellery_finish || "",
    jewellery_type: b.jewellery_type || "",
    theme_mood: b.theme_mood || "",
    text_style: b.text_style || "Normal",
    color_theme: b.color_theme || "",
    background_style: b.background_style || "",
    model_pref: b.model_pref || "",
    hero_types: Array.isArray(b.hero_types) && b.hero_types.length ? b.hero_types : ["normal"],
    hero_custom: b.hero_custom || "",
    crystal_name: b.crystal_name || "",
    crystal_benefit: b.crystal_benefit || "",
    offer: b.offer || "",
    details: b.details || "",
    notes: b.notes || "",
    ref_urls: b.ref_urls || [],
    inputs: b.inputs || { raw: [], crystal: [], refs: [], labels: [] }, // [{url, name}]
    status: "new",
    error: "",
    log: [`${new Date().toISOString()} created`],
    created: Date.now(),
  };
  await saveJob(job);
  return NextResponse.json(job);
}
