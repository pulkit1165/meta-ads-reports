// Crawl studdmuffyn.com public Shopify JSON endpoints into local JSON files.
import fs from 'fs';
import path from 'path';

const BASE = 'https://studdmuffyn.com';
const OUT = path.dirname(new URL(import.meta.url).pathname);

async function getJSON(url) {
  for (let i = 0; i < 4; i++) {
    try {
      const r = await fetch(url, { headers: { 'User-Agent': 'Mozilla/5.0' } });
      if (r.status === 429) { await new Promise(s => setTimeout(s, 3000)); continue; }
      if (!r.ok) throw new Error(`${r.status} ${url}`);
      return await r.json();
    } catch (e) {
      if (i === 3) throw e;
      await new Promise(s => setTimeout(s, 1500));
    }
  }
}

// 1. All products
const products = [];
for (let page = 1; page <= 40; page++) {
  const j = await getJSON(`${BASE}/products.json?limit=250&page=${page}`);
  if (!j.products.length) break;
  products.push(...j.products);
  console.log(`products page ${page}: +${j.products.length} (total ${products.length})`);
  await new Promise(s => setTimeout(s, 400));
}
fs.writeFileSync(path.join(OUT, 'products.json'), JSON.stringify(products, null, 1));

// 2. All collections + their product handles (ordering matters for merchandising)
const collections = [];
{
  let page = 1;
  while (true) {
    const j = await getJSON(`${BASE}/collections.json?limit=250&page=${page}`);
    if (!j.collections.length) break;
    collections.push(...j.collections);
    page++;
  }
}
console.log(`collections: ${collections.length}`);
const collectionProducts = {};
for (const c of collections) {
  const handles = [];
  for (let page = 1; page <= 20; page++) {
    let j;
    try {
      j = await getJSON(`${BASE}/collections/${c.handle}/products.json?limit=250&page=${page}`);
    } catch { break; }
    if (!j.products?.length) break;
    handles.push(...j.products.map(p => p.handle));
  }
  collectionProducts[c.handle] = handles;
  console.log(`collection ${c.handle}: ${handles.length} products`);
  await new Promise(s => setTimeout(s, 250));
}
fs.writeFileSync(path.join(OUT, 'collections.json'), JSON.stringify({ collections, collectionProducts }, null, 1));
console.log('DONE');
