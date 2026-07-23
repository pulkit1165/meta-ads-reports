"""Listing Studio — upload raw product photos, references and label shots;
get a full Shopify listing content package plus AI-generated listing images.

Run:  ./run.sh   →  local http://127.0.0.1:5757, LAN http://<mac-ip>:5757,
and (if the tunnel is up) a public https://…trycloudflare.com URL shown on
the dashboard.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from flask import (Flask, abort, redirect, render_template_string, request,
                   send_from_directory, session, url_for)

import pipeline
import store
from pipeline import JOBS_DIR, job_dir, read_job, write_job

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB per request

# --- team access -----------------------------------------------------------
# Team opens the public tunnel URL (or http://<mac-ip>:5757 on office Wi-Fi)
# and enters the password once per browser. Change via STUDIO_PASSWORD.
PASSWORD = os.environ.get("STUDIO_PASSWORD", "muffyn2026")
_secret_file = Path(__file__).parent / ".secret_key"
if not _secret_file.exists():
    _secret_file.write_bytes(os.urandom(32))
app.secret_key = _secret_file.read_bytes()

TUNNEL_FILE = Path(__file__).parent / "tunnel_url.txt"

ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif"}
CATEGORIES = ["Jewellery", "Skin Care", "Hair Care", "Crystals", "Crystal Decor",
              "Perfume", "Nutraceuticals", "Home Decor", "Other"]

BASE_CSS = """
:root{--ink:#221E1A;--paper:#FBF9F5;--gold:#A8842C;--gold-deep:#8A6B1F;--sand:#E8E2D6;
--mut:#7A7265;--card:#fff;--ok:#3F6C45;--err:#A33B2E}
*{box-sizing:border-box}
body{background:var(--paper);color:var(--ink);margin:0;padding:0 16px 80px;
font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:980px;margin:0 auto}
nav{max-width:980px;margin:0 auto 26px;display:flex;gap:6px;align-items:center;flex-wrap:wrap;
padding:14px 0;border-bottom:1px solid var(--sand)}
nav .brand{font-family:Palatino,Georgia,serif;font-weight:600;font-size:17px;margin-right:14px}
nav a{color:var(--ink);text-decoration:none;padding:7px 14px;border-radius:6px;font-size:14px;font-weight:600}
nav a:hover{background:#F1EADB}
nav a.on{background:var(--ink);color:#fff}
nav a.cta{background:var(--gold-deep);color:#fff;margin-left:auto}
h1,h2,h3{font-family:Palatino,"Palatino Linotype",Georgia,serif;font-weight:600}
h1{font-size:28px;margin:0 0 2px}
.eyebrow{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--gold-deep);font-weight:700}
.mut{color:var(--mut)} .small{font-size:13px}
a{color:var(--gold-deep)}
.card{background:var(--card);border:1px solid var(--sand);border-radius:8px;padding:20px 24px;margin:16px 0}
.btn{display:inline-block;background:var(--ink);color:#fff;border:0;border-radius:6px;
padding:10px 22px;font-size:14px;font-weight:600;cursor:pointer;text-decoration:none}
.btn.gold{background:var(--gold-deep)} .btn.ghost{background:#fff;color:var(--ink);border:1px solid var(--sand)}
.btn:disabled{opacity:.4;cursor:default}
input[type=text],input[type=number],input[type=password],textarea,select{width:100%;padding:9px 12px;
border:1px solid var(--sand);border-radius:6px;font:inherit;background:#fff}
label{font-size:12px;text-transform:uppercase;letter-spacing:.07em;color:var(--mut);font-weight:700;
display:block;margin:14px 0 4px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:0 20px}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}
.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}
@media(max-width:700px){.grid2,.grid3,.grid4{grid-template-columns:1fr}}
.stat{background:var(--card);border:1px solid var(--sand);border-radius:8px;padding:14px 18px}
.stat b{display:block;font-size:26px;font-family:Palatino,Georgia,serif;font-variant-numeric:tabular-nums}
.stat span{font-size:11.5px;color:var(--mut);text-transform:uppercase;letter-spacing:.07em}
.drop{border:2px dashed var(--sand);border-radius:8px;padding:18px;text-align:center;background:#FDFCF9}
.drop b{display:block;font-size:14px}
.drop span{font-size:12px;color:var(--mut)}
.thumbs{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.thumbs img{width:72px;height:72px;object-fit:cover;border-radius:6px;border:1px solid var(--sand)}
.status{display:inline-block;border-radius:99px;padding:3px 14px;font-size:12px;font-weight:700;
text-transform:uppercase;letter-spacing:.05em}
.status.new{background:#F1EADB;color:var(--gold-deep)}
.status.generating_content,.status.generating_images{background:#FDF0E3;color:#B45309}
.status.content_ready{background:#E8F0FB;color:#2B5B9E}
.status.done{background:#EAF0E8;color:var(--ok)}
.status.error{background:#FBEAE7;color:var(--err)}
table{width:100%;border-collapse:collapse;font-size:14px}
td,th{padding:8px 10px;border-top:1px solid var(--sand);text-align:left;vertical-align:top}
th{border-top:0;font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--gold-deep)}
.chip{display:inline-block;background:#F1EADB;border:1px solid var(--sand);border-radius:99px;
padding:2px 11px;font-size:12.5px;margin:0 5px 6px 0}
.imgs{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:16px}
.imgs .cell{background:var(--card);border:1px solid var(--sand);border-radius:8px;padding:12px}
.imgs img{width:100%;border-radius:6px;display:block}
.imgs .cap{font-size:12.5px;color:var(--mut);margin-top:8px}
pre.log{background:#211D18;color:#D8D2C6;border-radius:8px;padding:14px 18px;font-size:12px;
overflow-x:auto;max-height:260px;overflow-y:auto}
.descbox{max-width:68ch}
.descbox p{margin:.6em 0}
.pub{background:#EAF0E8;border:1px solid #C6D4C4;border-radius:8px;padding:12px 18px;font-size:14px}
.pub b{font-family:ui-monospace,Menlo,monospace}
.step{display:grid;grid-template-columns:44px 1fr;gap:0 14px;background:var(--card);
border:1px solid var(--sand);border-radius:8px;padding:16px 20px 14px 14px;margin:12px 0}
.step .n{font-family:Palatino,Georgia,serif;font-size:24px;color:var(--gold-deep);text-align:center;padding-top:2px}
.step h3{margin:0 0 3px;font-size:16px}
.step p{margin:.4em 0;font-size:14px}
.tag{display:inline-block;font-size:11px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;
border-radius:4px;padding:1px 8px;margin-left:6px}
.tag.free{background:#EAF0E8;color:var(--ok)} .tag.paid{background:#FDF0E3;color:#B45309}
.tablewrap{overflow-x:auto}
"""

LAYOUT = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title }} · Listing Studio</title><style>""" + BASE_CSS + """</style>
{% if refresh %}<meta http-equiv="refresh" content="6">{% endif %}
</head><body>
{% if nav %}<nav>
  <span class="brand">⛓ Listing Studio</span>
  <a href="/" class="{{ 'on' if active=='home' else '' }}">Dashboard</a>
  <a href="/listings" class="{{ 'on' if active=='listings' else '' }}">All listings</a>
  <a href="/guide" class="{{ 'on' if active=='guide' else '' }}">Rules &amp; guide</a>
  <a href="/new" class="cta">+ New product</a>
</nav>{% endif %}
<div class="wrap">
{{ body|safe }}
</div></body></html>"""


def page(title: str, body: str, refresh: bool = False, active: str = "", nav: bool = True) -> str:
    return render_template_string(LAYOUT, title=title, body=body, refresh=refresh,
                                  active=active, nav=nav)


def public_url() -> str:
    try:
        u = TUNNEL_FILE.read_text().strip()
        return u if u.startswith("https://") else ""
    except FileNotFoundError:
        return ""


@app.before_request
def require_login():
    if request.endpoint in ("login", "static"):
        return None
    if not session.get("ok"):
        return redirect(url_for("login", next=request.path))
    return None


@app.route("/login", methods=["GET", "POST"])
def login():
    err = ""
    if request.method == "POST":
        if request.form.get("password", "") == PASSWORD:
            session["ok"] = True
            session.permanent = True
            return redirect(request.args.get("next") or url_for("home"))
        err = "<p style='color:var(--err)'>Wrong password.</p>"
    body = f"""<div style="max-width:380px;margin:80px auto 0">
    <div class="eyebrow">Studd Muffyn · Internal tool</div>
    <h1>Listing Studio</h1>
    <form class="card" method="post">
      <label>Team password</label>
      <input type="password" name="password" autofocus>
      {err}
      <p style="margin-top:14px"><button class="btn gold">Enter</button></p>
    </form></div>"""
    return page("Login", body, nav=False)


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or f"product-{int(time.time())}"


def save_uploads(slug: str, field: str, subdir: str) -> int:
    n = 0
    d = job_dir(slug) / "inputs" / subdir
    d.mkdir(parents=True, exist_ok=True)
    for f in request.files.getlist(field):
        if not f or not f.filename:
            continue
        ext = Path(f.filename).suffix.lower()
        if ext not in ALLOWED_EXT:
            continue
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(f.filename).name)
        f.save(d / f"{int(time.time()*1000)}_{safe}")
        n += 1
    return n


def listing_rows(rows) -> str:
    out = ""
    for r in rows:
        title = f"<div class='small mut'>{r['title']}</div>" if r["title"] else ""
        out += (f"<tr><td><a href='/p/{r['slug']}'><b>{r['name']}</b></a>{title}</td>"
                f"<td>{r['category'] or '—'}</td>"
                f"<td>₹{r['price'] or '—'}</td>"
                f"<td>{r['images']}/6</td>"
                f"<td><span class='status {r['status']}'>{(r['status'] or '').replace('_',' ')}</span></td>"
                f"<td class='small mut'>{time.strftime('%d %b %H:%M', time.localtime(r['updated'] or 0))}</td></tr>")
    return out


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
@app.get("/")
def home():
    st = store.stats()
    recent = store.all_listings()[:6]
    pub = public_url()
    pub_html = (f"<div class='pub'>🌍 Team link (works anywhere): <b>{pub}</b> · password <b>{PASSWORD}</b></div>"
                if pub else
                "<div class='pub' style='background:#FDF0E3;border-color:#EDD3B3'>🌍 Public team link is offline — start it with <b>./run.sh</b> (it launches the tunnel automatically).</div>")
    rows = listing_rows(recent)
    body = f"""
    <div class="eyebrow">Dashboard</div>
    <h1>Products</h1>
    <p class="mut">Create a listing, watch its progress, and find every past listing here.</p>
    {pub_html}
    <div class="grid4" style="margin:18px 0">
      <div class="stat"><b>{st['total']}</b><span>total listings</span></div>
      <div class="stat"><b>{st['done']}</b><span>completed</span></div>
      <div class="stat"><b>{st['content_ready'] + st['new']}</b><span>in progress</span></div>
      <div class="stat"><b>{st['busy']}</b><span>generating now</span></div>
    </div>
    <p>
      <a class="btn gold" href="/new">+ New product</a>
      <a class="btn ghost" href="/listings">All listings</a>
      <a class="btn ghost" href="/guide">Rules &amp; guide</a>
    </p>
    <div class="card"><h3 style="margin-top:0">Recent</h3>
    {'<div class="tablewrap"><table><tr><th>Product</th><th>Category</th><th>Price</th><th>Images</th><th>Status</th><th>Updated</th></tr>' + rows + '</table></div>' if rows else '<p class="mut">Nothing yet — create the first product.</p>'}
    </div>"""
    return page("Dashboard", body, active="home")


# --------------------------------------------------------------------------
# All listings (database-backed)
# --------------------------------------------------------------------------
@app.get("/listings")
def listings():
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    rows = listing_rows(store.all_listings(q, status))
    opts = "".join(f"<option value='{s}' {'selected' if status == s else ''}>{s.replace('_',' ')}</option>"
                   for s in ("", "new", "generating_content", "content_ready", "generating_images", "done", "error"))
    body = f"""
    <div class="eyebrow">Database</div>
    <h1>All listings</h1>
    <form class="card" method="get" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      <input type="text" name="q" value="{q}" placeholder="Search name, SKU, title, category…" style="flex:1;min-width:220px">
      <select name="status" style="width:auto">{opts}</select>
      <button class="btn ghost">Filter</button>
      {'<a class="small" href="/listings">clear</a>' if (q or status) else ''}
    </form>
    <div class="card">
    {'<div class="tablewrap"><table><tr><th>Product</th><th>Category</th><th>Price</th><th>Images</th><th>Status</th><th>Updated</th></tr>' + rows + '</table></div>' if rows else '<p class="mut">No listings match.</p>'}
    </div>"""
    return page("All listings", body, active="listings")


# --------------------------------------------------------------------------
# Rules & guide
# --------------------------------------------------------------------------
@app.get("/guide")
def guide():
    body = """
    <div class="eyebrow">Team guide</div>
    <h1>Rules &amp; how it works</h1>

    <div class="step"><div class="n">1</div><div><h3>Collect material</h3>
      <p><b>Raw photos</b> — the actual product, sharp, plain background, 2–4 angles. Every AI image is built from these.</p>
      <p><b>References / inspo</b> — screenshots of listings whose <i>look</i> you want. Controls style, not product.</p>
      <p><b>Label shots</b> — clear photo of label/box; its text becomes the only source of claims. No label (jewellery)? Skip.</p>
    </div></div>
    <div class="step"><div class="n">2</div><div><h3>Create the product</h3>
      <p>+ New product → name, category, price, MRP, SKU, variants. <b>Notes</b> is your brief to the writer:
      USPs to push, things to avoid. More detail = fewer retries.</p>
    </div></div>
    <div class="step"><div class="n">3</div><div><h3>Generate content <span class="tag free">free</span></h3>
      <p>~2 minutes. You get title, description, SEO, tags, collections, variants, FAQs + the 6-image plan.
      Read the claims carefully — words are free to fix, images are not.</p>
    </div></div>
    <div class="step"><div class="n">4</div><div><h3>Refine <span class="tag free">free</span></h3>
      <p>Add a note ("shorter, focus on gifting") and generate content again until it reads right.</p>
    </div></div>
    <div class="step"><div class="n">5</div><div><h3>Generate images <span class="tag paid">~2 credits each</span></h3>
      <p>~5 minutes for the 6-image sequence in the Studd Muffyn style. One bad frame → use its own
      <b>Regenerate</b> button, don't re-run all six.</p>
    </div></div>
    <div class="step"><div class="n">6</div><div><h3>Final check</h3>
      <p>Review on the product page. The package (listing.json + images) is saved automatically;
      posting to Shopify is a coming one-click step.</p>
    </div></div>

    <div class="card"><h3 style="margin-top:0">House rules</h3>
    <div class="tablewrap"><table>
      <tr><th>Do</th><th>Don't</th></tr>
      <tr><td>Review content (free) before images</td><td>Generate images before copy is final</td></tr>
      <tr><td>Regenerate single images</td><td>Re-run all 6 for one bad frame</td></tr>
      <tr><td>Use real label photos for claims</td><td>Type ingredient claims from memory</td></tr>
      <tr><td>One product = one entry</td><td>Mix two products' photos in one job</td></tr>
    </table></div>
    <h3>If something's stuck</h3>
    <p class="small">"Generating…" — normal, the page refreshes itself. Red error chip — press the same button again;
    if it repeats, tell Pulkit. Site won't open — Pulkit's MacBook is off/asleep, or the tunnel is down.</p>
    </div>"""
    return page("Rules & guide", body, active="guide")


# --------------------------------------------------------------------------
# New product
# --------------------------------------------------------------------------
@app.get("/new")
def new_product():
    opts = "".join(f"<option>{c}</option>" for c in CATEGORIES)
    body = f"""
    <div class="eyebrow">Step 1 of 2 — inputs</div>
    <h1>New product</h1>
    <form class="card" method="post" action="{url_for('create')}" enctype="multipart/form-data">
      <div class="grid2">
        <div><label>Product name *</label><input type="text" name="name" required placeholder="e.g. Golden Cuban Chain"></div>
        <div><label>Category *</label><select name="category">{opts}</select></div>
        <div><label>Selling price (₹)</label><input type="number" name="price" step="0.01" placeholder="699"></div>
        <div><label>MRP / compare-at (₹)</label><input type="number" name="mrp" step="0.01" placeholder="999"></div>
        <div><label>SKU</label><input type="text" name="sku" placeholder="NTN1234"></div>
        <div><label>Variants (free text)</label><input type="text" name="variants" placeholder="Women 45cm ₹699 / Men 55cm ₹749"></div>
      </div>
      <label>Product details — everything the website content should say</label>
      <textarea name="details" rows="6" placeholder="Materials, sizes, weight, plating, ingredients, benefits, what's in the box, care instructions, target customer, occasion…&#10;e.g. 18K gold tone plated brass, herringbone snake design, 45cm & 55cm, anti-tarnish coating, lobster clasp, pack of 2, unisex, comes in Studd Muffyn pouch"></textarea>
      <label>Notes for the writer (tone, USPs to push, what to avoid…)</label>
      <textarea name="notes" rows="3" placeholder="e.g. focus on gifting angle, don't mention gold purity"></textarea>
      <div class="grid3" style="margin-top:18px">
        <div class="drop"><b>Raw product photos *</b><span>the actual product</span>
          <input type="file" name="raw" multiple accept="image/*"></div>
        <div class="drop"><b>References / inspo</b><span>existing listings, competitor pages</span>
          <input type="file" name="refs" multiple accept="image/*"></div>
        <div class="drop"><b>Label / packaging shots</b><span>ingredients, specs — becomes listing facts</span>
          <input type="file" name="labels" multiple accept="image/*"></div>
      </div>
      <label>Reference URLs (optional, one per line — e.g. an existing listing to match)</label>
      <textarea name="ref_urls" rows="2" placeholder="https://studdmuffyn.com/products/..."></textarea>
      <p style="margin-top:18px"><button class="btn gold" type="submit">Create product</button>
      <a class="btn ghost" href="/">Cancel</a></p>
    </form>"""
    return page("New product", body, active="")


@app.post("/create")
def create():
    name = request.form.get("name", "").strip()
    if not name:
        abort(400)
    slug = slugify(name)
    if job_dir(slug).exists():
        slug = f"{slug}-{int(time.time())%10000}"
    (job_dir(slug) / "output" / "images").mkdir(parents=True, exist_ok=True)
    job = {
        "slug": slug, "name": name,
        "category": request.form.get("category", "Other"),
        "price": request.form.get("price", ""), "mrp": request.form.get("mrp", ""),
        "sku": request.form.get("sku", ""), "variants": request.form.get("variants", ""),
        "details": request.form.get("details", ""),
        "notes": request.form.get("notes", ""),
        "ref_urls": [u.strip() for u in request.form.get("ref_urls", "").splitlines() if u.strip()],
        "status": "new", "error": "", "created": int(time.time()),
    }
    write_job(slug, job)
    for field, sub in (("raw", "raw"), ("refs", "refs"), ("labels", "labels")):
        save_uploads(slug, field, sub)
    pipeline.append_log(slug, "job created")
    return redirect(url_for("product", slug=slug))


# --------------------------------------------------------------------------
# Product page
# --------------------------------------------------------------------------
def _thumbs(slug: str, sub: str) -> str:
    d = job_dir(slug) / "inputs" / sub
    if not d.exists():
        return ""
    imgs = "".join(f"<img src='{url_for('input_file', slug=slug, sub=sub, fname=f.name)}'>"
                   for f in sorted(d.iterdir()) if f.suffix.lower() in ALLOWED_EXT)
    return f"<div class='thumbs'>{imgs}</div>" if imgs else ""


@app.get("/p/<slug>")
def product(slug):
    try:
        j = read_job(slug)
    except FileNotFoundError:
        abort(404)
    busy = j["status"] in ("generating_content", "generating_images")
    out = job_dir(slug) / "output"

    body = f"""
    <h1>{j['name']} <span class="status {j['status']}">{j['status'].replace('_',' ')}</span></h1>
    <p class="mut small">{j['category']} · ₹{j.get('price') or '—'} (MRP ₹{j.get('mrp') or '—'}) · SKU {j.get('sku') or '—'}</p>
    {f"<div class='card' style='border-color:#EDD3B3'><b style='color:var(--err)'>Error:</b> {j['error']}</div>" if j.get('error') else ''}
    """

    body += f"""<div class="card"><h3 style="margin-top:0">Inputs</h3>
    <div class="grid3">
      <div><b class="small">Raw photos</b>{_thumbs(slug,'raw') or "<p class='small mut'>none</p>"}</div>
      <div><b class="small">References</b>{_thumbs(slug,'refs') or "<p class='small mut'>none</p>"}</div>
      <div><b class="small">Labels</b>{_thumbs(slug,'labels') or "<p class='small mut'>none</p>"}</div>
    </div>
    <form method="post" action="{url_for('upload_more', slug=slug)}" enctype="multipart/form-data" style="margin-top:14px">
      <div class="grid3">
        <div class="drop"><b>+ raw</b><input type="file" name="raw" multiple accept="image/*"></div>
        <div class="drop"><b>+ refs</b><input type="file" name="refs" multiple accept="image/*"></div>
        <div class="drop"><b>+ labels</b><input type="file" name="labels" multiple accept="image/*"></div>
      </div>
      <p><button class="btn ghost" type="submit">Upload</button></p>
    </form></div>"""

    body += f"""<div class="card"><h3 style="margin-top:0">Product details &amp; brief</h3>
    <p class="small mut" style="margin-top:-6px">The content is written from this. Edit and press
    <b>1 · Generate content</b> again to rewrite.</p>
    <form method="post" action="{url_for('update_details', slug=slug)}">
      <label>Product details — everything the website content should say</label>
      <textarea name="details" rows="6">{j.get('details','')}</textarea>
      <label>Notes for the writer (tone, USPs, what to avoid)</label>
      <textarea name="notes" rows="3">{j.get('notes','')}</textarea>
      <p><button class="btn ghost" type="submit">Save details</button></p>
    </form></div>"""

    body += f"""<div class="card"><h3 style="margin-top:0">Generate</h3>
    <form method="post" action="{url_for('gen_content', slug=slug)}" style="display:inline">
      <button class="btn gold" {'disabled' if busy else ''}>1 · Generate content</button></form>
    <form method="post" action="{url_for('gen_images', slug=slug)}" style="display:inline;margin-left:8px">
      <button class="btn" {'disabled' if busy or not (out/'listing.json').exists() else ''}>2 · Generate images</button></form>
    <span class="mut small" style="margin-left:10px">{'Working… page auto-refreshes.' if busy else 'Content first (free), review it, then images (~2 credits each).'}</span>
    </div>"""

    lst = None
    if (out / "listing.json").exists():
        try:
            lst = json.loads((out / "listing.json").read_text())
        except Exception:
            body += "<div class='card'>listing.json exists but is not valid JSON — regenerate content.</div>"
    if lst:
        tags = "".join(f"<span class='chip'>{t}</span>" for t in lst.get("tags", []))
        colls = "".join(f"<span class='chip'>{c}</span>" for c in lst.get("collections", []))
        vrows = "".join(f"<tr><td>{v.get('title')}</td><td>{v.get('sku')}</td><td>₹{v.get('price')}</td>"
                        f"<td>₹{v.get('compare_at_price')}</td><td>{v.get('grams')} g</td></tr>"
                        for v in lst.get("variants", []))
        faqs = "".join(f"<p><b>{f.get('q')}</b><br><span class='mut'>{f.get('a')}</span></p>"
                       for f in lst.get("faqs", []))
        mf = lst.get("metafields", {})
        mfrows = "".join(f"<tr><td class='mut small' style='width:180px'>{k.replace('_',' ')}</td><td>{v}</td></tr>"
                         for k, v in mf.items())
        body += f"""<div class="card"><h2 style="margin-top:0">Listing content</h2>
        <div class="tablewrap"><table>
        <tr><td class="mut small" style="width:180px">Title</td><td><b>{lst.get('title')}</b></td></tr>
        <tr><td class="mut small">Handle</td><td>{lst.get('handle')}</td></tr>
        <tr><td class="mut small">Type / Vendor</td><td>{lst.get('product_type')} · {lst.get('vendor')}</td></tr>
        <tr><td class="mut small">Short description</td><td>{lst.get('short_description')}</td></tr>
        <tr><td class="mut small">SEO title</td><td>{lst.get('seo_title')}</td></tr>
        <tr><td class="mut small">SEO description</td><td>{lst.get('seo_description')}</td></tr>
        </table></div>
        <h3>Description</h3><div class="descbox">{lst.get('body_html','')}</div>
        <h3>Variants</h3><div class="tablewrap"><table><tr><th>Variant</th><th>SKU</th><th>Price</th><th>Compare-at</th><th>Weight</th></tr>{vrows}</table></div>
        <h3>Collections</h3><div>{colls}</div>
        <h3>Tags</h3><div>{tags}</div>
        <h3>Metafields</h3><div class="tablewrap"><table>{mfrows}</table></div>
        <h3>FAQs</h3>{faqs}
        <p class="small"><a href="{url_for('output_file', slug=slug, fname='listing.json')}">download listing.json</a></p>
        </div>"""

        cells = ""
        gen = {}
        if (out / "images.json").exists():
            try:
                gen = {g["slot"]: g for g in json.loads((out / "images.json").read_text())}
            except Exception:
                gen = {}
        for plan in lst.get("image_plan", []):
            n = plan.get("slot")
            g = gen.get(n)
            if g and (out / g.get("file", "")).exists():
                img_html = f"<img src='{url_for('output_file', slug=slug, fname=g['file'])}'>"
            elif busy:
                img_html = "<p class='mut small'>generating…</p>"
            else:
                img_html = "<p class='mut small'>not generated yet</p>"
            cells += f"""<div class="cell">{img_html}
              <div class="cap"><b>{n} · {plan.get('role')}</b><br>{plan.get('filename')}<br><i>{plan.get('alt')}</i></div>
              <form method="post" action="{url_for('gen_images', slug=slug)}">
                <input type="hidden" name="slots" value="{n}">
                <button class="btn ghost" style="margin-top:8px;padding:5px 12px;font-size:12px"
                 {'disabled' if busy else ''}>{'Regenerate' if g else 'Generate'} this</button>
              </form></div>"""
        body += f"<div class='card'><h2 style='margin-top:0'>Images</h2><div class='imgs'>{cells}</div></div>"

    logf = out / "log.txt"
    if logf.exists():
        tail = "".join(logf.read_text().splitlines(keepends=True)[-25:])
        body += f"<div class='card'><h3 style='margin-top:0'>Activity log</h3><pre class='log'>{tail}</pre></div>"

    return page(j["name"], body, refresh=busy, active="")


@app.post("/p/<slug>/details")
def update_details(slug):
    j = read_job(slug)
    j["details"] = request.form.get("details", "")
    j["notes"] = request.form.get("notes", "")
    write_job(slug, j)
    pipeline.append_log(slug, "details/notes updated")
    return redirect(url_for("product", slug=slug))


@app.post("/p/<slug>/upload")
def upload_more(slug):
    read_job(slug)
    n = sum(save_uploads(slug, f, s) for f, s in (("raw", "raw"), ("refs", "refs"), ("labels", "labels")))
    pipeline.append_log(slug, f"uploaded {n} more file(s)")
    return redirect(url_for("product", slug=slug))


@app.post("/p/<slug>/generate-content")
def gen_content(slug):
    j = read_job(slug)
    if j["status"] not in ("generating_content", "generating_images"):
        pipeline.start_async(pipeline.run_content_phase, slug)
    return redirect(url_for("product", slug=slug))


@app.post("/p/<slug>/generate-images")
def gen_images(slug):
    j = read_job(slug)
    if j["status"] not in ("generating_content", "generating_images"):
        slots = None
        if request.form.get("slots"):
            slots = [int(s) for s in request.form["slots"].split(",")]
        pipeline.start_async(pipeline.run_images_phase, slug, slots)
    return redirect(url_for("product", slug=slug))


@app.get("/p/<slug>/inputs/<sub>/<path:fname>")
def input_file(slug, sub, fname):
    if sub not in ("raw", "refs", "labels"):
        abort(404)
    return send_from_directory(job_dir(slug) / "inputs" / sub, fname)


@app.get("/p/<slug>/output/<path:fname>")
def output_file(slug, fname):
    return send_from_directory(job_dir(slug) / "output", fname)


if __name__ == "__main__":
    JOBS_DIR.mkdir(exist_ok=True)
    r = pipeline.recover_stuck()
    n = store.rebuild(JOBS_DIR)
    print(f"listing index rebuilt: {n} listings ({r} recovered)")
    app.run(host=os.environ.get("STUDIO_HOST", "0.0.0.0"), port=5757, debug=False)
