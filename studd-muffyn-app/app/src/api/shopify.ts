// Headless Shopify data layer.
// Boots instantly from the bundled catalog snapshot (real crawled data),
// then silently refreshes from studdmuffyn.com's public Shopify JSON
// endpoints so prices / availability / new launches stay live.
// Checkout is 100% Shopify via cart permalinks.
import catalogJson from '../data/catalog.json';
import type { Catalog, Product, Collection } from './types';

export const BASE = 'https://studdmuffyn.com';

const catalog = catalogJson as unknown as Catalog;

const byHandle = new Map<string, Product>();
for (const p of catalog.products) byHandle.set(p.handle, p);

export function allProducts(): Product[] {
  return catalog.products;
}

export function allCollections(): Collection[] {
  return catalog.collections;
}

export function getCollection(handle: string): Collection | undefined {
  return catalog.collections.find((c) => c.handle === handle);
}

export function getProduct(handle: string): Product | undefined {
  return byHandle.get(handle);
}

export function collectionProducts(handle: string): Product[] {
  const handles = catalog.collectionProducts[handle] || [];
  return handles.map((h) => byHandle.get(h)).filter(Boolean) as Product[];
}

export function nav() {
  return catalog.nav;
}

// ---- live refresh -------------------------------------------------------

function mapApiProduct(p: any): Product {
  const v0 = p.variants?.[0] || {};
  return {
    id: p.id,
    handle: p.handle,
    title: p.title,
    vendor: p.vendor,
    productType: p.product_type,
    tags: Array.isArray(p.tags) ? p.tags : String(p.tags || '').split(', ').filter(Boolean),
    price: parseFloat(v0.price ?? '0'),
    compareAt: v0.compare_at_price ? parseFloat(v0.compare_at_price) : null,
    images: (p.images || []).map((i: any) => i.src),
    variants: (p.variants || []).map((v: any) => ({
      id: v.id,
      title: v.title,
      price: parseFloat(v.price),
      compareAt: v.compare_at_price ? parseFloat(v.compare_at_price) : null,
      available: v.available !== false,
      sku: v.sku,
      option1: v.option1,
      option2: v.option2,
    })),
    options: (p.options || []).map((o: any) => ({ name: o.name, values: o.values })),
    descriptionHtml: p.body_html || '',
    createdAt: p.created_at || '',
  };
}

/** Refresh a product live from Shopify (price / stock). Falls back silently. */
export async function fetchLiveProduct(handle: string): Promise<Product | undefined> {
  try {
    const r = await fetch(`${BASE}/products/${handle}.json`);
    if (!r.ok) throw new Error(String(r.status));
    const j = await r.json();
    const p = mapApiProduct(j.product);
    byHandle.set(handle, p);
    return p;
  } catch {
    return byHandle.get(handle);
  }
}

/** Refresh a collection's product list live (merchandised order from Shopify). */
export async function fetchLiveCollection(handle: string): Promise<Product[] | undefined> {
  try {
    const r = await fetch(`${BASE}/collections/${handle}/products.json?limit=250`);
    if (!r.ok) throw new Error(String(r.status));
    const j = await r.json();
    const products: Product[] = j.products.map(mapApiProduct);
    for (const p of products) byHandle.set(p.handle, p);
    catalog.collectionProducts[handle] = products.map((p) => p.handle);
    return products;
  } catch {
    return undefined;
  }
}

/** Shopify cart permalink → real Shopify checkout (payments, coupons, shipping).
 * UTM params tag the order so app sales are identifiable in Shopify admin
 * (order → Conversion summary) and Analytics (utm_source=studd_muffyn_app). */
export function checkoutUrl(lines: { variantId: number; qty: number }[], discount?: string) {
  const path = lines.map((l) => `${l.variantId}:${l.qty}`).join(',');
  const params = new URLSearchParams({ utm_source: 'studd_muffyn_app', utm_medium: 'mobile_app' });
  if (discount) params.set('discount', discount);
  return `${BASE}/cart/${path}?${params.toString()}`;
}

const APP_API = 'https://studd-muffyn-app.vercel.app';

/** Preferred checkout: Shiprocket (Fastrr) — same 1-click checkout the website
 * uses. Falls back to the Shopify cart-permalink checkout if Fastrr isn't
 * configured or is unreachable, so checkout always works. */
export async function startCheckoutUrl(
  lines: { variantId: number; qty: number }[],
  discount?: string
): Promise<string> {
  const shopifyFallback = checkoutUrl(lines, discount);
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 6000);
    const r = await fetch(`${APP_API}/api/fastrr-checkout`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ items: lines }),
      signal: ctrl.signal,
    });
    clearTimeout(t);
    if (r.ok) {
      const j = await r.json();
      if (j?.token) {
        return `${APP_API}/checkout.html?token=${encodeURIComponent(j.token)}&fallback=${encodeURIComponent(shopifyFallback)}`;
      }
    }
  } catch {}
  return shopifyFallback;
}

// ---- lightweight client-side search -------------------------------------

export function searchProducts(query: string, limit = 30): Product[] {
  const q = query.trim().toLowerCase();
  if (!q) return [];
  const terms = q.split(/\s+/);
  const scored: { p: Product; s: number }[] = [];
  for (const p of catalog.products) {
    const hay = `${p.title} ${p.productType} ${p.tags.join(' ')}`.toLowerCase();
    let s = 0;
    for (const t of terms) {
      if (!hay.includes(t)) { s = 0; break; }
      s += p.title.toLowerCase().includes(t) ? 3 : 1;
      if (p.title.toLowerCase().startsWith(t)) s += 2;
    }
    if (s > 0) scored.push({ p, s });
  }
  scored.sort((a, b) => b.s - a.s);
  return scored.slice(0, limit).map((x) => x.p);
}
