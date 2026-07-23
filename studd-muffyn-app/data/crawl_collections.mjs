// Resume crawl: collections + per-collection product handle order. 429-resilient.
import fs from 'fs';
import path from 'path';

const BASE = 'https://studdmuffyn.com';
const OUT = path.dirname(new URL(import.meta.url).pathname);

async function getJSON(url) {
  for (let i = 0; i < 10; i++) {
    try {
      const r = await fetch(url, { headers: { 'User-Agent': 'Mozilla/5.0' } });
      if (r.status === 429 || r.status >= 500) {
        await new Promise(s => setTimeout(s, 4000 + i * 3000));
        continue;
      }
      if (r.status === 404) return null;
      if (!r.ok) throw new Error(`${r.status} ${url}`);
      return await r.json();
    } catch (e) {
      await new Promise(s => setTimeout(s, 3000));
    }
  }
  console.error('GIVING UP', url);
  return null;
}

const collections = [];
for (let page = 1; page <= 10; page++) {
  const j = await getJSON(`${BASE}/collections.json?limit=250&page=${page}`);
  if (!j || !j.collections.length) break;
  collections.push(...j.collections);
  await new Promise(s => setTimeout(s, 600));
}
console.log(`collections: ${collections.length}`);

// resume support
const outFile = path.join(OUT, 'collections.json');
let collectionProducts = {};
if (fs.existsSync(outFile)) {
  try { collectionProducts = JSON.parse(fs.readFileSync(outFile)).collectionProducts || {}; } catch {}
}

let n = 0;
for (const c of collections) {
  n++;
  if (collectionProducts[c.handle]) continue;
  const handles = [];
  for (let page = 1; page <= 20; page++) {
    const j = await getJSON(`${BASE}/collections/${c.handle}/products.json?limit=250&page=${page}`);
    if (!j || !j.products?.length) break;
    handles.push(...j.products.map(p => p.handle));
    await new Promise(s => setTimeout(s, 500));
  }
  collectionProducts[c.handle] = handles;
  if (n % 10 === 0) {
    fs.writeFileSync(outFile, JSON.stringify({ collections, collectionProducts }));
    console.log(`progress ${n}/${collections.length}`);
  }
  await new Promise(s => setTimeout(s, 500));
}
fs.writeFileSync(outFile, JSON.stringify({ collections, collectionProducts }, null, 1));
console.log('DONE', collections.length, 'collections');
