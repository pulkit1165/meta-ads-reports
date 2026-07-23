// Build app/src/data/catalog.json from crawl output.
// Slims products, ensures every collection referenced by the app UI has
// its product list (fetches any missing ones directly), bundles nav.
import fs from 'fs';
import path from 'path';

const DATA = path.dirname(new URL(import.meta.url).pathname);
const APP = path.join(DATA, '..', 'app');
const BASE = 'https://studdmuffyn.com';

const products = JSON.parse(fs.readFileSync(path.join(DATA, 'products.json')));
let collections = [];
let collectionProducts = {};
try {
  const c = JSON.parse(fs.readFileSync(path.join(DATA, 'collections.json')));
  collections = c.collections || [];
  collectionProducts = c.collectionProducts || {};
} catch {}
const nav = JSON.parse(fs.readFileSync(path.join(DATA, 'nav.json')));

// handles referenced by the app UI
const uiFiles = [
  path.join(APP, 'src/config/home.json'),
  path.join(APP, 'app/(tabs)/categories.tsx'),
];
const needed = new Set();
for (const f of uiFiles) {
  const src = fs.readFileSync(f, 'utf8');
  for (const m of src.matchAll(/\/collections\/([a-z0-9-]+)/g)) needed.add(m[1]);
  for (const m of src.matchAll(/"handle":\s*"([a-z0-9-]+)"/g)) needed.add(m[1]);
}
console.log('UI-referenced collections:', needed.size);

async function getJSON(url) {
  for (let i = 0; i < 8; i++) {
    const r = await fetch(url, { headers: { 'User-Agent': 'Mozilla/5.0' } });
    if (r.status === 429 || r.status >= 500) { await new Promise((s) => setTimeout(s, 3500)); continue; }
    if (r.status === 404) return null;
    return await r.json();
  }
  return null;
}

for (const h of needed) {
  if (collectionProducts[h]?.length) continue;
  const j = await getJSON(`${BASE}/collections/${h}/products.json?limit=250`);
  if (j?.products) {
    collectionProducts[h] = j.products.map((p) => p.handle);
    console.log('fetched', h, collectionProducts[h].length);
  } else {
    console.log('MISSING', h);
  }
  if (!collections.find((c) => c.handle === h)) {
    const cj = await getJSON(`${BASE}/collections/${h}.json`);
    if (cj?.collection) {
      const c = cj.collection;
      collections.push({
        id: c.id, title: c.title, handle: c.handle, description: c.description || '',
        image: c.image?.src || null, products_count: c.products_count ?? 0,
      });
    }
  }
  await new Promise((s) => setTimeout(s, 350));
}

const knownHandles = new Set(products.map((p) => p.handle));

const slim = products.map((p) => ({
  id: p.id,
  handle: p.handle,
  title: p.title,
  vendor: p.vendor,
  productType: p.product_type,
  tags: p.tags || [],
  price: parseFloat(p.variants?.[0]?.price ?? '0'),
  compareAt: p.variants?.[0]?.compare_at_price ? parseFloat(p.variants[0].compare_at_price) : null,
  images: (p.images || []).slice(0, 8).map((i) => i.src),
  variants: (p.variants || []).map((v) => ({
    id: v.id,
    title: v.title,
    price: parseFloat(v.price),
    compareAt: v.compare_at_price ? parseFloat(v.compare_at_price) : null,
    available: v.available !== false,
    sku: v.sku || undefined,
    option1: v.option1,
    option2: v.option2,
  })),
  options: (p.options || []).map((o) => ({ name: o.name, values: o.values })),
  descriptionHtml: (p.body_html || '').slice(0, 12000),
  createdAt: p.created_at || '',
}));

const slimCollections = collections.map((c) => ({
  id: c.id,
  handle: c.handle,
  title: c.title,
  description: c.description || '',
  image: c.image?.src || c.image || null,
  productsCount: c.products_count ?? 0,
}));

// keep only handles we actually have product data for
const cp = {};
for (const [h, arr] of Object.entries(collectionProducts)) {
  cp[h] = arr.filter((x) => knownHandles.has(x));
}

const catalog = {
  products: slim,
  collections: slimCollections,
  collectionProducts: cp,
  nav,
  crawledAt: new Date().toISOString(),
};
const out = path.join(APP, 'src/data/catalog.json');
fs.writeFileSync(out, JSON.stringify(catalog));
console.log('catalog.json written:', (fs.statSync(out).size / 1e6).toFixed(2), 'MB');
console.log('collections in catalog:', slimCollections.length, '| with product lists:', Object.keys(cp).length);
const missing = [...needed].filter((h) => !cp[h]?.length);
console.log('UI handles still empty:', missing.join(', ') || 'none');
