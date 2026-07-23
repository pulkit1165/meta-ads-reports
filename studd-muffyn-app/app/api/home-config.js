// Vercel serverless function: turns studdmuffyn.com's live homepage into the
// app's home config. The website IS the merchandising dashboard — edit the
// homepage in Shopify's theme customizer and the app follows within minutes.
// GET /api/home-config  →  { announcement, sections[], generatedAt, source }

const SITE = 'https://studdmuffyn.com';
const CACHE_SECONDS = 600; // CDN caches the response for 10 min

// Site sections that link to the sister site — map to on-store collections.
const EXTERNAL_HANDLE_MAP = {
  'skin-care': 'skin-care-bestsellers',
  'hair-care1': 'hair-care-bestsellers',
  'perfumes-1': 'perfume-best-sellers',
  nutraceuticals: 'nutraceuticals',
};

const titleCache = new Map(); // warm-instance cache of collection titles

const prettify = (h) =>
  h.replace(/-/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()).replace(/\bAnd\b/g, '&');

async function fetchText(url) {
  const r = await fetch(url, { headers: { 'User-Agent': 'Mozilla/5.0 (iPhone; like Mac OS X) StuddMuffynApp' } });
  if (!r.ok) throw new Error(`${r.status} ${url}`);
  return r.text();
}

async function collectionTitle(handle) {
  if (titleCache.has(handle)) return titleCache.get(handle);
  try {
    const r = await fetch(`${SITE}/collections/${handle}.json`);
    if (r.ok) {
      const j = await r.json();
      const t = j.collection && j.collection.title;
      if (t) {
        titleCache.set(handle, t);
        return t;
      }
    }
  } catch {}
  const t = prettify(handle);
  titleCache.set(handle, t);
  return t;
}

// ---- HTML helpers -----------------------------------------------------------

function firstImages(seg, limit) {
  const out = [];
  const seen = new Set();
  const re = /(?:srcset|data-srcset|src|data-src)="([^"]*\/cdn\/shop\/[^"]*)"/g;
  let m;
  while ((m = re.exec(seg)) && out.length < limit) {
    let u = m[1].split(',')[0].trim().split(' ')[0];
    if (u.startsWith('//')) u = 'https:' + u;
    const base = u.split('?')[0];
    if (!/\.(jpe?g|png|webp)$/i.test(base) || seen.has(base)) continue;
    seen.add(base);
    const v = (u.match(/[?&]v=(\d+)/) || [])[1];
    out.push(`${base}?${v ? `v=${v}&` : ''}width=1200`);
  }
  return out;
}

function imgAspect(seg, imageUrl) {
  // find width/height attrs on the <img> that references this file
  const file = imageUrl.split('/').pop().split('?')[0];
  const re = new RegExp(`<img[^>]*${file.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}[^>]*>`, 'i');
  const tag = (seg.match(re) || [])[0];
  if (!tag) return null;
  const w = (tag.match(/\bwidth="(\d+)"/) || [])[1];
  const h = (tag.match(/\bheight="(\d+)"/) || [])[1];
  if (w && h && +h > 0) return Math.round((+w / +h) * 100) / 100;
  return null;
}

function collectionLinks(seg) {
  const out = [];
  const seen = new Set();
  const re = /href="(?:https?:\/\/([a-z.]+))?\/collections\/([a-z0-9-]+)/g;
  let m;
  while ((m = re.exec(seg))) {
    const domain = m[1];
    let handle = m[2];
    if (domain && !domain.includes('studdmuffyn.com')) {
      handle = EXTERNAL_HANDLE_MAP[handle];
      if (!handle) continue;
    }
    if (!seen.has(handle)) {
      seen.add(handle);
      out.push(handle);
    }
  }
  return out;
}

function linkImagePairs(seg) {
  // href → next cdn image within the anchor's chunk
  const pairs = [];
  const chunks = seg.split(/<a\s/i).slice(1);
  for (const c of chunks) {
    const hrefM = c.match(/^[^>]*href="([^"]+)"/i);
    if (!hrefM) continue;
    const href = hrefM[1];
    const imgs = firstImages(c.slice(0, 3000), 1);
    if (!imgs.length) continue;
    let handle = null;
    const cm = href.match(/^(?:https?:\/\/([a-z.]+))?\/collections\/([a-z0-9-]+)/);
    if (cm) {
      handle = cm[2];
      if (cm[1] && !cm[1].includes('studdmuffyn.com')) handle = EXTERNAL_HANDLE_MAP[handle] || null;
    }
    if (handle) pairs.push({ handle, image: imgs[0], aspect: imgAspect(c, imgs[0]) });
  }
  // dedupe by handle
  const seen = new Set();
  return pairs.filter((p) => (seen.has(p.handle) ? false : (seen.add(p.handle), true)));
}

// ---- main parser ------------------------------------------------------------

async function buildConfig(html) {
  const sections = [];

  // announcement bar
  const announcements = [];
  const annRe = /announcement[^>]*>([^<>{}]{6,140})</g;
  let am;
  while ((am = annRe.exec(html))) {
    const t = am[1].replace(/\s+/g, ' ').trim();
    if (t && !announcements.includes(t) && !/^\s*(function|var|window)/.test(t)) announcements.push(t);
  }

  // split into theme sections, preserving page order
  const parts = html.split(/(?=<section[^>]+id="shopify-section-template--\d+__)/).slice(1);
  const railHandles = [];

  for (const p of parts) {
    const name = (p.match(/id="shopify-section-template--\d+__([a-zA-Z0-9_-]+)"/) || [])[1] || '';

    if (name.startsWith('featured_collection')) {
      const handle = collectionLinks(p)[0];
      if (handle) {
        sections.push({ type: 'productRail', handle, title: null });
        railHandles.push(handle);
      }
    } else if (name.startsWith('image_hero') || name.startsWith('slideshow')) {
      const imgs = firstImages(p, 4);
      const links = collectionLinks(p);
      if (imgs.length >= 1) {
        if (imgs.length === 1 || name.startsWith('image_hero')) {
          const aspect = imgAspect(p, imgs[0]);
          sections.push({
            type: 'imageBanner',
            image: imgs[0],
            url: links[0] ? `/collections/${links[0]}` : undefined,
            aspect: aspect || 2,
          });
        } else {
          sections.push({
            type: 'hero',
            aspect: imgAspect(p, imgs[0]) || 1.7,
            slides: imgs.map((im, i) => ({
              image: im,
              url: links[i] ? `/collections/${links[i]}` : links[0] ? `/collections/${links[0]}` : '/',
            })),
          });
        }
      }
    } else if (name.startsWith('scrolling_content')) {
      const imgs = firstImages(p, 8);
      if (imgs.length) {
        const a = imgAspect(p, imgs[0]) || 2;
        sections.push({ type: 'logoStrip', images: imgs, height: a > 3 ? 56 : a > 1.6 ? 110 : 140 });
      }
    } else if (name.startsWith('blocks')) {
      const pairs = linkImagePairs(p);
      if (pairs.length >= 2) {
        sections.push({
          type: 'categoryGrid',
          aspect: pairs[0].aspect || 1,
          showLabel: false,
          items: pairs.slice(0, 8).map((x) => ({ title: prettify(x.handle), handle: x.handle, image: x.image })),
        });
      }
    } else {
      // custom sections (e.g. the top offer-tile row): many link+image pairs
      const pairs = linkImagePairs(p);
      if (pairs.length >= 3) {
        sections.push({ type: 'iconRow', items: pairs.slice(0, 10).map((x) => ({ image: x.image, handle: x.handle })) });
      }
    }
  }

  // resolve rail titles (warm-cached; fallback = prettified handle)
  await Promise.all(
    railHandles.map((h) =>
      Promise.race([collectionTitle(h), new Promise((res) => setTimeout(() => res(prettify(h)), 4000))])
    )
  );
  for (const s of sections) {
    if (s.type === 'productRail') s.title = titleCache.get(s.handle) || prettify(s.handle);
  }

  sections.push({ type: 'recentlyViewed', title: 'Recently Viewed' });

  return {
    version: 3,
    source: 'live-website',
    generatedAt: new Date().toISOString(),
    announcement: {
      messages: announcements.length ? announcements.slice(0, 3) : ['FREE SHIPPING ON PREPAID ORDERS'],
    },
    sections,
  };
}

module.exports = async (req, res) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  try {
    const html = await fetchText(`${SITE}/`);
    const cfg = await buildConfig(html);
    // sanity: a homepage should yield a healthy number of sections
    if (!cfg.sections || cfg.sections.length < 4) throw new Error('parse produced too few sections');
    res.setHeader('Cache-Control', `s-maxage=${CACHE_SECONDS}, stale-while-revalidate=86400`);
    res.status(200).json(cfg);
  } catch (e) {
    // fail soft: the app falls back to its bundled config
    res.setHeader('Cache-Control', 's-maxage=60');
    res.status(503).json({ error: String(e && e.message ? e.message : e) });
  }
};

module.exports.buildConfig = buildConfig; // exported for local testing
