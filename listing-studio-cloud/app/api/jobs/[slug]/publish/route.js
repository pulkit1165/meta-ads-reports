import { NextResponse } from "next/server";
import { getJob, saveJob, jobOutputs } from "../../../../../lib/store";

// One-click "Post to Shopify (Draft)" — creates the product on the chosen store
// with full copy, variants, tags, SEO and all generated images (Shopify pulls
// the images from the public Blob URLs).

const STORES = {
  SML: { domain: "studdmuffynlife.myshopify.com", env: "SHOPIFY_TOKEN_SML" },
  NBP: { domain: "472d21.myshopify.com", env: "SHOPIFY_TOKEN_NBP" },
  SM: { domain: "studd-muffyn.myshopify.com", env: "SHOPIFY_TOKEN_SM" },
};

export async function POST(req, { params }) {
  const { slug } = await params;
  const { store } = await req.json();
  const cfg = STORES[store];
  if (!cfg) return NextResponse.json({ error: "unknown store" }, { status: 400 });
  const token = process.env[cfg.env];
  if (!token) return NextResponse.json({ error: `${store} token not connected yet` }, { status: 400 });

  const job = await getJob(slug);
  if (!job) return NextResponse.json({ error: "not found" }, { status: 404 });
  const { listing, images } = await jobOutputs(slug);
  if (!listing) return NextResponse.json({ error: "generate content first" }, { status: 400 });

  const altBySlot = {};
  for (const p of listing.image_plan || []) altBySlot[p.slot] = p.alt || "";
  const imgs = images
    .map((im) => ({ url: im.url, slot: Number(im.pathname.match(/slot(\d+)/)?.[1] || 99) }))
    .sort((a, b) => a.slot - b.slot);

  const variants = (listing.variants?.length ? listing.variants : [{ title: "Default Title" }])
    .map((v) => ({
      option1: v.title || "Default Title",
      sku: v.sku || job.sku || "",
      price: String(v.price ?? job.price ?? "0"),
      compare_at_price: v.compare_at_price ? String(v.compare_at_price) : undefined,
      grams: v.grams || 0,
      taxable: true,
    }));

  // storefront accordion blocks — same metafields the existing listings use
  const MARKETED_BY = "Marketed By: Nature Touch Nutrition\nSECOND FLOOR, 241, LINK ROAD, STREET NO.3, Dashmesh Nagar, Ludhiana, Ludhiana, Punjab, 141003.";
  const pb = listing.page_blocks || {};
  const faqText = (listing.faqs || [])
    .map((f, i) => `${i + 1}. ${f.q}\n${f.a}`).join("\n\n");
  const single = (k, v) => v ? { namespace: "custom", key: k, value: String(v), type: "single_line_text_field" } : null;
  const multi = (k, v) => v ? { namespace: "custom", key: k, value: String(v), type: "multi_line_text_field" } : null;
  const metafields = [
    pb.subtitle ? { namespace: "descriptors", key: "subtitle", value: pb.subtitle, type: "single_line_text_field" } : null,
    multi("product_brief", pb.product_brief),
    multi("what_we_put_in_", pb.what_we_put_in),
    multi("product_benefits", pb.product_benefits),
    multi("product_details", pb.product_details),
    multi("product_specification", pb.product_specification),
    multi("how_to_use_", pb.how_to_use),
    multi("faq", faqText),
    multi("manufactured_marketed_by", MARKETED_BY),
    ...(pb.detail_points || []).slice(0, 4).map((v, i) => single(`product_detail_new_${i + 1}`, v)),
    ...(pb.reduces || []).slice(0, 3).map((v, i) => single(`reduces${i + 1}`, v)),
  ].filter(Boolean);

  const payload = {
    product: {
      metafields,
      title: listing.title,
      body_html: listing.body_html || "",
      vendor: listing.vendor || "Studd Muffyn",
      product_type: listing.product_type || "",
      tags: (listing.tags || []).join(", "),
      status: "draft",
      options: [{ name: "Size", values: variants.map((v) => v.option1) }],
      variants,
      images: imgs.map((im, i) => ({ src: im.url, position: i + 1, alt: altBySlot[im.slot] || listing.title })),
      // Shopify hard limits: SEO title 70 chars, SEO description 320 chars
      metafields_global_title_tag: (listing.seo_title || "").slice(0, 70) || undefined,
      metafields_global_description_tag: (listing.seo_description || "").slice(0, 320) || undefined,
    },
  };

  const r = await fetch(`https://${cfg.domain}/admin/api/2024-10/products.json`, {
    method: "POST",
    headers: { "X-Shopify-Access-Token": token, "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    return NextResponse.json({ error: `Shopify ${r.status}: ${JSON.stringify(body.errors || body).slice(0, 300)}` }, { status: 502 });
  }

  const product = body.product;
  const adminUrl = `https://${cfg.domain.replace(".myshopify.com", "")}.myshopify.com/admin/products/${product.id}`;
  job.published = job.published || {};
  job.published[store] = { product_id: product.id, admin_url: adminUrl, at: Date.now() };
  job.log.push(`${new Date().toISOString()} published draft to ${store} (product ${product.id})`);
  await saveJob(job);
  return NextResponse.json({ ok: true, store, product_id: product.id, admin_url: adminUrl });
}
