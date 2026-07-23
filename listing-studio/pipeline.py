"""Listing Studio pipeline — runs Claude Code headlessly to turn uploaded
product inputs (raw photos, reference/inspo images, label shots) into a full
Shopify listing content package and AI-generated listing images.

Two phases, each a separate `claude -p` run:
  content — read inputs, OCR labels, write output/listing.json
  images  — read listing.json image_plan, generate via Higgsfield MCP,
            download results into output/images/
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

import store

STUDIO_DIR = Path(__file__).resolve().parent
JOBS_DIR = STUDIO_DIR / "jobs"

# One pipeline run at a time per job
_locks: dict[str, threading.Lock] = {}


def _lock(slug: str) -> threading.Lock:
    return _locks.setdefault(slug, threading.Lock())


def job_dir(slug: str) -> Path:
    return JOBS_DIR / slug


def read_job(slug: str) -> dict:
    return json.loads((job_dir(slug) / "job.json").read_text())


def write_job(slug: str, data: dict) -> None:
    (job_dir(slug) / "job.json").write_text(json.dumps(data, indent=2, ensure_ascii=False))
    store.upsert(data, job_dir(slug))


def append_log(slug: str, line: str) -> None:
    with open(job_dir(slug) / "output" / "log.txt", "a") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {line}\n")


CONTENT_PROMPT = """You are the content engine of Listing Studio, Studd Muffyn's product-listing
generator. Work ONLY inside the job directory: {job_path}

INPUTS
- {job_path}/job.json — operator-entered product info. Key fields and how to use them:
  · category, jewellery_finish, jewellery_type — what the product IS; shots must suit the
    type (earrings → on-ear close-ups; ring → on-hand; bangle/bracelet → on-wrist;
    chain/pendant → neckline; home decor → styled room surface; perfume → vanity set)
  · theme_mood, color_theme, background_style — MUST drive every image_plan "theme":
    respect the stated mood, palette and background in all slots
  · model_pref — "No model shots" = zero model slots; "Women"/"Men"/"Both" = model slots
    for exactly those; "Both" = one of each
  · hero_types, offer, hero_custom, crystal_name, crystal_benefit — hero slots, see below
  · details / notes — factual claims and the writer's brief
- {job_path}/inputs/raw/ — real photos of the product. Look at every one.
- {job_path}/inputs/labels/ — photos of the product label/packaging. Read ALL text off them
  (ingredients, specs, weights, warnings, directions). This is the source of truth for claims.
- {job_path}/inputs/refs/ — reference/inspiration images (existing listings, competitor pages,
  mood boards). Use them for tone and style, never copy text verbatim.

TASK
Write {job_path}/output/listing.json — the complete Shopify listing content package.
Perfect Hinglish-free premium Indian D2C English, matching the tone of studdmuffyn.com.
Never invent ingredient/material claims not supported by the label photos or operator notes.

The JSON must have exactly these keys:
{{
  "title": str,                     // display title
  "handle": str,                    // url-safe kebab
  "product_type": str,
  "vendor": str,                    // EXACTLY job.json "vendor" (fallback "Studd Muffyn")
  "short_description": str,         // 1-2 sentences
  "body_html": str,                 // full description, valid HTML, <p>/<ul>/<strong> only
  "seo_title": str,                 // <= 60 chars
  "seo_description": str,           // <= 160 chars
  "tags": [str],                    // 15-25 incl. common misspellings buyers type
  "collections": [str],
  "variants": [{{"title": str, "sku": str, "price": num, "compare_at_price": num, "grams": num}}],
  "metafields": {{"highlights": str, "material_or_ingredients": str, "care_or_directions": str,
                 "shipping": "Ships in 24 hrs · COD available · Free shipping over ₹499",
                 "returns": "7-day easy replacement for manufacturing defects"}},
  "faqs": [{{"q": str, "a": str}}],  // 4-6
  "page_blocks": {{                  // fills the storefront accordion blocks — copy the
                                    // exact style of existing studdmuffyn.com listings
    "subtitle": str,                //  descriptors.subtitle — "Benefit | Benefit | Benefit"
    "product_brief": str,           //  2-3 sentence brief for the Description block
    "what_we_put_in": str,          //  "Crafted With" block — one line per material or
                                    //  ingredient, each starting "✦ Name – benefit it adds"
    "product_benefits": str,        //  "Benefits" block — 4-6 lines "✨ Benefit headline
                                    //  followed by one plain sentence"
    "product_details": str,         //  "Key: Value" lines (Plating/Base Metal/Color/Finish
                                    //  for jewellery; Skin Type/Texture/Quantity for care)
    "detail_points": [str],         //  exactly 4 one-liners, each starting with one emoji,
                                    //  the product's 4 strongest USPs
    "reduces": [str],               //  exactly 3 short chips: what the product reduces or
                                    //  gives (e.g. "Dullness" / "Easy Styling")
    "product_specification": str,   //  "Product Specification" block — "Key: Value" lines:
                                    //  Product Name / Design / Material / Net Quantity /
                                    //  Country of Origin: India — only true facts
    "how_to_use": str               //  numbered steps, 2-4 lines
  }},
  "image_plan": [                   // exactly 11 slots — the master sequence below
    {{"slot": 1, "role": str, "filename": str, "alt": str,
      "theme": str,                 // pinpointed visual theme: exact background, surface,
                                    // lighting, mood, palette — e.g. "white silk drape,
                                    // soft daylight, cream + gold palette, airy premium"
      "overlay_text": str,          // the EXACT words that must appear ON the photo,
                                    // "" if the photo has no text. e.g.
                                    // "18K Gold Tone Plating | Anti-Tarnish | Pack of 2"
      "text_style": str,            // typography spec for the overlay text — copy the
                                    // spec for the operator's chosen TEXT STYLE (job.json
                                    // "text_style", see TEXT STYLE OPTIONS below);
                                    // "" when overlay_text is empty
      "prompt": str}}               // Higgsfield generation prompt; every prompt must say the
                                    // generated item must match the referenced real product
                                    // exactly and follow the brand's bright premium style
  ]
}}

IMAGE STYLE — every image_plan prompt must describe a premium Indian D2C AD CREATIVE,
not a plain catalog photo. House style (Studd Muffyn):
- ONE benefit-led headline across the top ("Advanced Hydration for Modern Skin"
  energy — 4-8 words), set per the TEXT STYLE OPTIONS spec below. Put the exact
  headline in overlay_text.
- The brand's OFFICIAL logo small in the top-left corner (the real logo file is
  attached as reference media at generation time — prompts must say "the exact
  brand logo from the reference, never re-typeset").
- The product LARGE and front-facing, label crisp and readable, real pack from the
  reference photos.
- The scene built from ingredient/benefit props that tell the product's story (fruits,
  botanicals, water splashes, textures for skin/hair; silk, stone, styled sets for
  jewellery) — arranged around the product, colorful and abundant.
- Vibrant, saturated, high-contrast light — energetic and scroll-stopping, never washed
  out, never muted corporate grey.
- Respect the operator's theme_mood / color_theme / background_style choices; when inspo
  references are given, copy their composition and typography treatment.
Exception: a slot whose role demands a clean no-text frame (the "normal" hero,
craftsmanship close-ups, on-model shots) skips the headline but keeps the vibrant
brand energy.

TEXT STYLE OPTIONS — job.json has "text_style" (operator's pick). Every slot's
"text_style" field must carry the matching spec below (adapted to the slot's colors).
NEVER use rounded bubble fonts or amorphous blob/splash shapes behind words — text
sits on flat bands, thin-outline pills, or directly on the background.

JEWELLERY LOCK (overrides the operator's pick): when category is Gold Jewellery or
Crystal Jewellery, or jewellery_type is set, EVERY slot's text_style must be exactly
this house spec — "headline in an elegant high-contrast serif (Didot/Playfair energy),
ALL CAPS with wide letter-spacing, deep maroon-brown (dark chocolate) ink, centered;
supporting captions and body lines in a clean light geometric sans-serif, warm dark
grey, centered, generous line spacing; text sits directly on the cream/ivory ground
(no bands, no pills, no boxes); the product may sit inside a soft rounded-corner
photo card". Backgrounds for jewellery text frames default to warm cream/ivory.
- "Normal": clean geometric sans-serif, medium weight, sentence case, dark ink or
  white text on simple flat bands, generous letter-spacing, subtle soft shadow.
- "Minimal": thin light-weight sans-serif, small caps or lowercase, wide tracking,
  no boxes or bands at all — text floats on whitespace, maximum restraint.
- "Luxury": refined high-contrast serif (Didot/Playfair energy) for headlines with
  letter-spaced sans captions, thin gold hairline rules above/below, ivory/gold/ink
  palette, editorial jewellery-magazine look.
- "Loud": very large bold condensed sans-serif in ALL CAPS, tight leading, high
  contrast (ink on white / white on black), one accent color word, poster energy.
- "Funny": clean sans base with ONE playful hand-lettered accent word or arrow/
  underline doodle, tilted sticker-style captions, light-hearted but still tidy.
- "Offer": price-first layout — huge price and offer percentage in heavy sans,
  strike-through MRP beside it, bold ribbon/badge shapes (straight-edged, not
  blobs), urgency words in caps.

HERO SLOTS — job.json has "hero_types" (a list; may contain several). Create ONE hero
image slot per listed type, in this order, as the FIRST slots of image_plan (they fill
master-sequence slot 1; extra hero types push the sequence down — total slots may exceed
11 when several hero types are selected):
- "normal": clean premium product hero, brand-style bright set, overlay_text "" (no text)
- "offer": product hero with a bold offer ribbon/badge; overlay_text = job.json "offer"
  verbatim — the exact offer and price, spelled precisely
- "crystal": the CRYSTAL PENDANT TEMPLATE hero frame described below (ribbon badge +
  crystal zoom circle + benefit callout)
- "custom": follow job.json "hero_custom" exactly — it is the operator's own description;
  if it names on-image text, put that text in overlay_text verbatim

MASTER IMAGE SEQUENCE — every listing gets these 11 slots, in this exact order.
Each slot must look CLEARLY different from its neighbours (different set, angle,
layout) — no two slots may read as the same photo with different text:
 1. hero — per hero_types above (price/offer/USPs when the type says so)
 2. usp-details — product with 3-4 short USP callout chips + key details around it
    (benefit-led: what the buyer GETS). overlay_text = the exact chip texts.
 3. breakdown-material — "anatomy" frame: finishing & material called out with thin
    leader lines to the parts (e.g. plating, base metal, clasp, link type; for skincare:
    key ingredients on the label). overlay_text = the exact part labels.
 4. quality — craftsmanship/quality proof frame: macro of the finish with 2-3 quality
    claims (e.g. "Anti-tarnish coating", "Nickel & lead free", "Hand-finished").
    Distinct from slot 3: slot 3 says WHAT it is made of, slot 4 proves HOW WELL.
 5. closeup — pure macro detail shots, razor sharp, NO text. A collage of 2-3 crops
    is allowed (clasp, texture, edge).
 6. durability — strength & durability test visual: the product being stress-tested
    in a believable way (chain pulled taut between fingers, worn under running water,
    bend/twist test; capsules/skincare: lab-test energy). overlay_text = one short
    test claim (e.g. "Pull tested · Waterproof · Sweat proof").
 7. faq — clean FAQ card layout: pick the 3 shortest, most buying-decision faqs from
    "faqs" and render them as Q/A cards. overlay_text = those exact Q&A lines.
 8. measurement — product on a clean scale/ruler frame with dimension callouts (length,
    pendant size, weight). ONLY use numbers given in the job form/notes/variants —
    NEVER invent a measurement; if none are provided, show the tape-measure frame with
    the size options as text (e.g. "45 cm | 50 cm") from the variants.
 9. on-model — worn/in-use by a model matching the audience; if the product is unisex,
    ONE photo with BOTH a man and a woman wearing it. Bright airy set, no text.
10. reviews — social proof: flat-lay or set with three 5-star review cards
    (overlay_text = the exact review words, natural Indian buyer names).
11. product-shots — clean premium catalog shot (multi-angle collage or styled flat-lay),
    minimal or no text — the "just show me the product" frame.
Category adaptations: skincare/haircare swap slot 9 to model applying the product and
slot 6 to texture/efficacy proof; crystals & decor swap slot 9 for lifestyle placement
in a styled home and slot 6 for size-scale in hand.

CRYSTAL PENDANT TEMPLATE (use whenever job.json has a non-empty "crystal_name"; the
crystal's macro photos are in inputs/crystal/). These frames REPLACE the matching
master-sequence slots — the other master slots still apply, 11 total:
- slot 1 hero — "crystal callout" frame: product (pendant on its chain) draped over a round
  white pedestal on a bright white/cream set; a ribbon badge in the top-left corner reading
  "<CRYSTAL_NAME> CRYSTAL" (pink/rose ribbon, small crystal icon); a large circular zoom
  inset on the right showing the infused crystal in macro detail, connected to the pendant
  by a dotted line, with a grey pill label "<CRYSTAL_NAME> CRYSTAL" and the crystal_benefit
  line below it. overlay_text must be:
  "<CRYSTAL_NAME> CRYSTAL | <CRYSTAL_NAME> CRYSTAL | <crystal_benefit>"
- slot 2 usp-details — the crystal macro centred with 3 short benefit callouts around it
  (overlay_text = those exact callouts; this doubles as the crystal-meaning frame)
- slot 5 closeup — macro of pendant + crystal setting, no text
- slot 9 on-model — pendant worn at the collarbone, bright airy set, no text
- slot 10 reviews — flat-lay with three 5-star review cards (overlay_text = the review words)

Write the file with the Write tool. Reply with just: DONE or ERROR <one line reason>.
"""

IMAGES_PROMPT = """You are the image engine of Listing Studio. Work in: {job_path}

The Higgsfield MCP tools you need are named (note the claude_ai prefix, all lowercase):
  mcp__claude_ai_higgsfield__media_upload
  mcp__claude_ai_higgsfield__media_import_url
  mcp__claude_ai_higgsfield__generate_image   (model: nano_banana_pro)
  mcp__claude_ai_higgsfield__job_status
Load them with ToolSearch using query "higgsfield" (they are deferred tools). If the first
ToolSearch returns nothing, wait 10 seconds and try again with query "generate_image" and
query "media_upload" before concluding they are unavailable.

1. Read {job_path}/output/listing.json — the "image_plan" array.
   SLOTS TO GENERATE: {only_clause}. Generate EXACTLY these slot numbers and no others —
   match each requested number against the "slot" field in image_plan and use THAT entry's
   prompt. Never substitute a different slot.
2. Reference media, PER SLOT:
   - BRAND LOGO (every slot that shows a logo): import ONCE via media_import_url the
     official logo for job.json "vendor" and attach it as an extra reference to every
     text/branded slot. In each prompt say: "the small logo in the top-left corner must
     be THIS exact logo from the reference — same lettering, layout and mark; never
     re-typeset, redraw or invent the brand name". Logo URLs:
       Studd Muffyn    → https://sn34bsjv5pejb0um.public.blob.vercel-storage.com/brand-logos/studd-muffyn.png
       studdmuffynlife → https://sn34bsjv5pejb0um.public.blob.vercel-storage.com/brand-logos/studdmuffynlife.png
       Nuskhe By Paras → https://sn34bsjv5pejb0um.public.blob.vercel-storage.com/brand-logos/nuskhe-by-paras.png
       Big Pucchi      → https://sn34bsjv5pejb0um.public.blob.vercel-storage.com/brand-logos/big-pucchi.png
   - If the image_plan entry has a "refs" array (objects with url/kind), import those
     URLs with media_import_url and use THEM as that slot's reference media.
     kind "raw" = the product itself (must match exactly); kind "inspo" = style/mood
     reference (copy its look, lighting and composition — not its product).
     Mention in the prompt which reference is the product and which is the style guide.
   - Otherwise fall back to 1-2 of the best photos from {job_path}/inputs/raw/
     via media_upload. If {job_path}/inputs/crystal/ has photos, ALSO upload the best
     one and include it as an extra reference for any slot that shows the crystal
     zoom/macro (the crystal in the generated image must match it exactly).
3. For each image_plan slot, compose the generation prompt as:
   the slot's "prompt" + " THEME: " + its "theme" (if present) +
   (if "overlay_text" is non-empty) " The image must display EXACTLY this text,
   spelled precisely, no other text: '<overlay_text>'. TYPOGRAPHY: <text_style —
   or, when empty: premium minimal editorial type — clean geometric sans-serif,
   medium weight (never extra-bold, never rounded/bubbly), generous letter-spacing,
   dark ink or white text; labels sit on simple flat bands, thin-outline pills or
   directly on the background with a subtle shadow — NEVER on amorphous blob or
   splash shapes>. The TYPOGRAPHY note is a styling instruction only — NEVER render
   the font name or the word TYPOGRAPHY as visible text in the image."
   (if "overlay_text" is empty) " The image must contain NO text at all."
   Call generate_image with model "nano_banana_pro", aspect_ratio "1:1", that
   composed prompt, and that slot's reference media (medias role "image").
   Poll job_status until completed.

   SPEED (hard requirement — the whole phase must finish in ~5 minutes):
   - Import/upload the reference media ONCE up front.
   - Then SUBMIT the generate_image call for EVERY required slot immediately,
     back-to-back, WITHOUT waiting for any to complete — Higgsfield runs them in
     parallel. Only after all are submitted, poll job_status for each pending job
     in rounds until all complete.
   - Download all finished images in one parallel step (single bash command with
     backgrounded curls + wait), not one by one.
   - Do not re-read files or re-verify anything that is already confirmed.

   AD-CREATIVE LOOK: these are scroll-stopping D2C ad creatives, not catalog shots —
   clear confident headlines where the plan specifies overlay_text, brand logo
   top-left, rich ingredient/benefit props, saturated vibrant colors, high contrast.
   Text treatment stays premium and editorial: flat bands, thin gold rules or
   outline pills only — no blob/splash shapes behind words, no bubbly lettering.
   If inspo reference media is provided, match its composition and typography
   treatment closely.

   PRODUCT FIDELITY (non-negotiable, applies to every slot): the product in the
   generated image must be IDENTICAL to the raw reference photo — same design,
   pattern, links/texture, clasp/closure, stones, engraving, proportions, color and
   finish. The AI may change ONLY the scene around it: background, surface, lighting,
   props, model, angle, composition. It must never redesign, "improve", simplify,
   thicken, thin, recolor, add elements to or remove elements from the product itself.
   Append this instruction to every generation prompt you send, e.g.:
   "Reproduce the product from the reference photo exactly — do not alter its design,
   proportions, texture, clasp or color in any way; change only the scene."
   After each generation, compare the result against the raw reference; if the product
   visibly differs (wrong pattern, wrong clasp, wrong thickness, wrong color), retry
   that slot once with a stronger fidelity instruction before accepting it.
4. Download each result PNG into {job_path}/output/images/ named slot<N>_<filename-stem>.png
   (curl is fine) — <N> MUST be the "slot" number from image_plan that the image was
   generated for, and <filename-stem> comes from that entry's "filename".
5. Update {job_path}/output/images.json: a list of
   {{"slot": N, "file": "images/<name>.png", "url": rawUrl, "generation_id": id}}.
   Merge with existing entries if the file already exists (replace same slot).

If the Higgsfield tools truly cannot be loaded or credits run out, reply ERROR <reason>.
Otherwise reply DONE after all slots are saved.
"""


def _run_claude(slug: str, prompt: str, timeout: int = 1800, model: str | None = None) -> tuple[bool, str]:
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    jp = str(job_dir(slug))
    cmd = [
        claude_bin, "-p", prompt,
        "--output-format", "json",
        "--permission-mode", "bypassPermissions",
        "--add-dir", jp,
    ]
    if model:
        cmd += ["--model", model]
    append_log(slug, f"claude -p started (timeout {timeout}s)")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=jp)
    except subprocess.TimeoutExpired:
        return False, "claude -p timed out"
    except FileNotFoundError:
        return False, "claude CLI not found (set CLAUDE_BIN)"
    if proc.returncode != 0:
        return False, f"claude exit {proc.returncode}: {(proc.stderr or '')[-300:]}"
    try:
        text = str(json.loads(proc.stdout).get("result", "")).strip()
    except json.JSONDecodeError:
        text = (proc.stdout or "").strip()
    ok = "DONE" in text.upper()[:2000] and not text.upper().startswith("ERROR")
    return ok, text[-500:]


def _set_status(slug: str, status: str, error: str = "") -> None:
    job = read_job(slug)
    job["status"] = status
    job["error"] = error
    write_job(slug, job)


def run_content_phase(slug: str) -> None:
    with _lock(slug):
        _set_status(slug, "generating_content")
        append_log(slug, "content phase started")
        ok, msg = _run_claude(slug, CONTENT_PROMPT.format(job_path=job_dir(slug)))
        listing_ok = (job_dir(slug) / "output" / "listing.json").exists()
        if ok and listing_ok:
            _set_status(slug, "content_ready")
            append_log(slug, "content phase done")
        else:
            _set_status(slug, "error", f"content: {msg}")
            append_log(slug, f"content phase FAILED: {msg}")


def run_images_phase(slug: str, only_slots: list[int] | None = None) -> None:
    with _lock(slug):
        _set_status(slug, "generating_images")
        only = (f"only slot number(s) {only_slots}" if only_slots else "every slot in image_plan")
        append_log(slug, f"images phase started ({only})")
        prompt = IMAGES_PROMPT.format(job_path=job_dir(slug), only_clause=only)
        ok, msg = _run_claude(slug, prompt)
        if not ok and "unavailable" in msg.lower():
            # transient MCP-connector hiccup — one automatic retry
            append_log(slug, "Higgsfield tools not visible — retrying in 30s")
            time.sleep(30)
            ok, msg = _run_claude(slug, prompt)
        # what's on disk decides success — the reply text is only a fallback signal
        have = list((job_dir(slug) / "output" / "images").glob("*.png"))
        if have:
            _set_status(slug, "done")
            append_log(slug, f"images phase done ({len(have)} files)")
        else:
            _set_status(slug, "error", f"images: {msg}")
            append_log(slug, f"images phase FAILED: {msg}")


def start_async(target, *args) -> None:
    threading.Thread(target=target, args=args, daemon=True).start()


def recover_stuck() -> int:
    """Generation threads die with the process, so any job still marked
    generating_* at startup is stale. Reset it based on what's on disk."""
    n = 0
    for p in JOBS_DIR.iterdir() if JOBS_DIR.exists() else []:
        jf = p / "job.json"
        if not jf.exists():
            continue
        try:
            job = json.loads(jf.read_text())
        except Exception:
            continue
        if job.get("status") not in ("generating_content", "generating_images"):
            continue
        imgs = list((p / "output" / "images").glob("*.png")) if (p / "output" / "images").exists() else []
        if imgs:
            job["status"] = "done"
        elif (p / "output" / "listing.json").exists():
            job["status"] = "content_ready"
        else:
            job["status"] = "new"
        job["error"] = ""
        write_job(job["slug"], job)
        append_log(job["slug"], f"recovered after app restart → {job['status']}")
        n += 1
    return n
