// Scrape per-product "extras" the public JSON API doesn't expose:
// Judge.me reviews, Pairs-well-with, and theme-rendered detail sections
// (Product Description / Key Highlights / Hero Ingredients / Product Benefits…).
// Runs with a real browser (Playwright) because plain fetches get bot-blocked.
//
//   node scrape_extras.mjs            # all products, resumable (skips existing)
//   node scrape_extras.mjs --limit 50 # first 50 missing
//   node scrape_extras.mjs --force    # re-scrape everything
//
// Output: data/extras/<handle>.json + data/extras-index.json

import fs from 'fs';
import path from 'path';
import { chromium, devices } from 'playwright';

const DATA = path.dirname(new URL(import.meta.url).pathname);
const OUT = path.join(DATA, 'extras');
const CATALOG = JSON.parse(fs.readFileSync(path.join(DATA, '..', 'app', 'src', 'data', 'catalog.json')));
const HOME = JSON.parse(fs.readFileSync(path.join(DATA, '..', 'app', 'src', 'config', 'home.json')));
const EXEC = process.env.HOME + '/Library/Caches/ms-playwright/chromium_headless_shell-1228/chrome-headless-shell-mac-arm64/chrome-headless-shell';

const FORCE = process.argv.includes('--force');
const LIMIT = (() => { const i = process.argv.indexOf('--limit'); return i > -1 ? +process.argv[i + 1] : Infinity; })();

fs.mkdirSync(OUT, { recursive: true });

// priority: products featured on the home config first
const priority = new Set();
for (const sec of HOME.sections || []) {
  if (sec.handle) for (const h of CATALOG.collectionProducts[sec.handle] || []) priority.add(h);
}
const handles = [...CATALOG.products.map((p) => p.handle)].sort(
  (a, b) => (priority.has(b) ? 1 : 0) - (priority.has(a) ? 1 : 0)
);

const strip = (s) =>
  s
    .replace(/<br\s*\/?>/gi, '\n')
    .replace(/<\/(p|div|li|h[1-6]|tr)>/gi, '\n')
    .replace(/<li[^>]*>/gi, '• ')
    .replace(/<[^>]+>/g, '')
    .replace(/&amp;/g, '&').replace(/&nbsp;/g, ' ').replace(/&#39;|&rsquo;/g, "'")
    .replace(/&quot;|&ldquo;|&rdquo;/g, '"').replace(/&gt;/g, '>').replace(/&lt;/g, '<')
    .replace(/[ \t]+/g, ' ')
    .replace(/\n{3,}/g, '\n\n')
    .trim();

const SECTION_HEADINGS = [
  'Product Description', 'Key Highlights', 'Product Details', 'Hero Ingredients',
  'Product Benefits', 'How to Use', 'How To Use', 'FAQ', 'Frequently Asked Questions',
];

function parseExtras(html, handle) {
  // reviews
  const reviews = [];
  const revRe = /<div class="jdgm-rev jdgm[^"]*"([\s\S]{0,4000}?)(?=<div class="jdgm-rev jdgm|jdgm-rev-widg__footer|$)/g;
  let m;
  while ((m = revRe.exec(html)) && reviews.length < 12) {
    const b = m[1];
    const score = +((b.match(/data-score="(\d)"/) || [])[1] || 0);
    const author = strip((b.match(/jdgm-rev__author">([^<]*)</) || [, ''])[1]);
    const date = ((b.match(/jdgm-rev__timestamp[^>]*data-content="([^"]+)"/) || [])[1] || '').slice(0, 10);
    const title = strip((b.match(/jdgm-rev__title">([\s\S]*?)<\/b>/) || [, ''])[1] || '');
    const body = strip((b.match(/jdgm-rev__body">([\s\S]*?)<\/div>/) || [, ''])[1] || '');
    if (score && (body || title)) reviews.push({ score, author, date, title, body: body.slice(0, 600) });
  }
  let rating = +((html.match(/data-average-rating="([\d.]+)"/) || [])[1] || 0);
  let reviewCount = +((html.match(/data-number-of-reviews="(\d+)"/) || [])[1] || 0);
  if (!rating && reviews.length) rating = +(reviews.reduce((s, r) => s + r.score, 0) / reviews.length).toFixed(2);
  if (!reviewCount) reviewCount = reviews.length;

  // pairs well with
  let pairsWith = [];
  const pi = html.indexOf('Pairs well with');
  if (pi > -1) {
    const seg = html.slice(pi, pi + 40000);
    pairsWith = [...new Set([...seg.matchAll(/\/products\/([a-z0-9-]+)/g)].map((x) => x[1]))]
      .filter((h) => h !== handle)
      .slice(0, 6);
  }

  // theme detail sections
  const sections = [];
  for (const head of SECTION_HEADINGS) {
    const hi = html.search(new RegExp(`<h[1-4][^>]*>\\s*(?:<[^>]+>\\s*)*${head.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`, 'i'));
    if (hi === -1) continue;
    let seg = html.slice(hi, hi + 20000);
    seg = seg.replace(/^<h[1-4][^>]*>[\s\S]*?<\/h[1-4]>/, '');
    // stop at the next known heading
    let cut = seg.length;
    for (const other of SECTION_HEADINGS) {
      if (other === head) continue;
      const oi = seg.search(new RegExp(`<h[1-4][^>]*>\\s*(?:<[^>]+>\\s*)*${other.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`, 'i'));
      if (oi > -1 && oi < cut) cut = oi;
    }
    const jd = seg.indexOf('jdgm-widget');
    if (jd > -1 && jd < cut) cut = jd;
    const text = strip(seg.slice(0, cut)).slice(0, 4000);
    if (text.length > 40 && !sections.find((s) => s.heading.toLowerCase() === head.toLowerCase()))
      sections.push({ heading: head, text });
  }

  return { handle, rating, reviewCount, reviews, pairsWith, sections, scrapedAt: new Date().toISOString() };
}

const todo = handles.filter((h) => FORCE || !fs.existsSync(path.join(OUT, h + '.json'))).slice(0, LIMIT);
console.log(`scraping ${todo.length} of ${handles.length} products`);

const browser = await chromium.launch({ executablePath: EXEC });
const CONCURRENCY = 3;
let done = 0, fail = 0;

async function worker(queue) {
  const ctx = await browser.newContext({ ...devices['iPhone 13'] });
  const page = await ctx.newPage();
  await page.route('**/*', (route) => {
    const t = route.request().resourceType();
    return ['image', 'media', 'font'].includes(t) ? route.abort() : route.continue();
  });
  let h;
  while ((h = queue.shift())) {
    try {
      await page.goto(`https://studdmuffyn.com/products/${h}`, { waitUntil: 'domcontentloaded', timeout: 45000 });
      // nudge lazy widgets (reviews) into rendering
      await page.mouse.wheel(0, 4000).catch(() => {});
      await page.waitForSelector('div.jdgm-rev', { timeout: 6000 }).catch(() => {});
      await page.waitForTimeout(800);
      const html = await page.content();
      const extras = parseExtras(html, h);
      fs.writeFileSync(path.join(OUT, h + '.json'), JSON.stringify(extras));
      done++;
      if (done % 20 === 0) console.log(`progress ${done}/${todo.length} (fail ${fail})`);
    } catch (e) {
      fail++;
      console.log('FAIL', h, String(e).slice(0, 80));
    }
    await page.waitForTimeout(300);
  }
  await ctx.close();
}

const queue = [...todo];
await Promise.all(Array.from({ length: CONCURRENCY }, () => worker(queue)));
await browser.close();

// index: rating + count per handle (for product cards)
const index = {};
for (const f of fs.readdirSync(OUT)) {
  if (!f.endsWith('.json')) continue;
  try {
    const j = JSON.parse(fs.readFileSync(path.join(OUT, f)));
    if (j.reviewCount) index[j.handle] = { r: j.rating, n: j.reviewCount };
  } catch {}
}
fs.writeFileSync(path.join(DATA, 'extras-index.json'), JSON.stringify(index));
console.log(`DONE scraped=${done} failed=${fail} indexed=${Object.keys(index).length}`);
