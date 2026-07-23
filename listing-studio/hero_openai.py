#!/usr/bin/env python3
"""
Hero-image trial via OpenAI gpt-image-1 (the same image model ChatGPT uses).

Takes a real product photo + price/MRP/USPs and renders the Listing Studio
hero block layout (see artifact "Hero Image Block Spec"): brand strip top-left,
offer badge top-right, product centre-right, USP callouts left, price block
bottom-left, trust strip along the bottom.

Usage:
  OPENAI_API_KEY=sk-... python3 hero_openai.py \
      --image jobs/test-golden-rice-chain/inputs/raw/1783341629799_velore_ref.jpg \
      --product "VELORE Snake Chain - 18K Gold Tone Plated" \
      --price 399 --mrp 799 --offer "50% OFF" \
      --usps "Anti-tarnish guarantee|18K gold tone plating|Waterproof - shower safe" \
      -n 2 --quality high

Key resolution order: --key flag, OPENAI_API_KEY env, .env.openai next to this file.
Output: hero_openai_<n>.png next to this script (or --outdir).
"""
import argparse, base64, os, sys, time
import requests

PROMPT_TEMPLATE = """Premium Indian D2C e-commerce HERO AD CREATIVE, square 1:1.

PRODUCT (exact match required): use the product from the attached reference photo
EXACTLY as it is - same design, same links/texture, same clasp, same color and
finish. Do not redesign, simplify, or substitute the product. Place it as the
dominant subject in the CENTRE-RIGHT ~55% of the frame, elegantly draped/arranged,
studio lit, razor sharp.

BACKGROUND: bright white-to-warm-cream seamless studio backdrop, soft premium
shadow under the product, subtle depth. No props that hide the product.

TEXT LAYOUT - render ALL text crisply, correctly spelled, no gibberish:
1. TOP-LEFT: small elegant brand wordmark "STUDD MUFFYN" in thin dark letter-spaced caps.
2. TOP-RIGHT: a bold offer badge/ribbon reading "{offer}" - deep gold/brass badge, white text.
3. LEFT COLUMN (vertically stacked, aligned left, thin leader lines pointing to the product):
   three USP callout chips, rounded pills with subtle gold outline, dark text:
{usp_lines}
4. BOTTOM-LEFT PRICE BLOCK: large bold price "₹{price}" in dark ink, next to it the
   old price "₹{mrp}" smaller with a clean strikethrough line through it.
5. BOTTOM STRIP: thin light band with small icons + text: "COD Available"  ·
   "Free Shipping"  ·  "6-Month Warranty".

STYLE: premium minimal advertising layout, generous whitespace, consistent gold/brass
accent color, modern sans-serif for prices and chips. Looks like a top D2C brand's
paid-social hero. Product name context: {product}."""


def build_prompt(product, price, mrp, offer, usps):
    usp_lines = "\n".join(f'   - "{u.strip()}"' for u in usps)
    return PROMPT_TEMPLATE.format(product=product, price=price, mrp=mrp,
                                  offer=offer, usp_lines=usp_lines)


def resolve_key(flag):
    if flag:
        return flag
    if os.environ.get("OPENAI_API_KEY"):
        return os.environ["OPENAI_API_KEY"]
    envf = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env.openai")
    if os.path.exists(envf):
        for line in open(envf):
            if line.strip().startswith("OPENAI_API_KEY="):
                return line.strip().split("=", 1)[1]
    sys.exit("No OpenAI API key. Pass --key, set OPENAI_API_KEY, or put "
             "OPENAI_API_KEY=sk-... in listing-studio/.env.openai")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True, help="real product photo (reference)")
    ap.add_argument("--product", required=True)
    ap.add_argument("--price", required=True)
    ap.add_argument("--mrp", required=True)
    ap.add_argument("--offer", default="LIMITED TIME OFFER")
    ap.add_argument("--usps", required=True, help="pipe-separated, e.g. 'A|B|C'")
    ap.add_argument("-n", type=int, default=1, help="number of variants")
    ap.add_argument("--quality", default="high", choices=["low", "medium", "high"])
    ap.add_argument("--size", default="1024x1024")
    ap.add_argument("--outdir", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--key")
    args = ap.parse_args()

    key = resolve_key(args.key)
    prompt = build_prompt(args.product, args.price, args.mrp, args.offer,
                          args.usps.split("|"))
    print(prompt, "\n" + "=" * 60)

    mime = "image/png" if args.image.lower().endswith(".png") else "image/jpeg"
    for i in range(1, args.n + 1):
        t0 = time.time()
        with open(args.image, "rb") as f:
            r = requests.post(
                "https://api.openai.com/v1/images/edits",
                headers={"Authorization": f"Bearer {key}"},
                files={"image[]": (os.path.basename(args.image), f, mime)},
                data={"model": "gpt-image-1", "prompt": prompt,
                      "size": args.size, "quality": args.quality, "n": 1},
                timeout=300,
            )
        if r.status_code != 200:
            sys.exit(f"OpenAI error {r.status_code}: {r.text[:500]}")
        b64 = r.json()["data"][0]["b64_json"]
        out = os.path.join(args.outdir, f"hero_openai_{i}.png")
        open(out, "wb").write(base64.b64decode(b64))
        usage = r.json().get("usage", {})
        print(f"saved {out}  ({time.time()-t0:.0f}s, quality={args.quality}, "
              f"tokens={usage.get('total_tokens', '?')})")


if __name__ == "__main__":
    main()
