"use client";
import { useState } from "react";
import { upload } from "@vercel/blob/client";

const LISTING_TYPES = ["Skin Care", "Hair Care", "Crystal Home Decor", "Crystal Jewellery",
  "Perfume", "Gold Jewellery", "Other"];
const JEWELLERY_TYPES = ["Earring", "Bracelet", "Bangle", "Chain", "Pendant + Chain", "Ring", "Other"];
const HERO_TYPES = [
  { key: "normal", label: "Normal", hint: "clean premium product shot, no text" },
  { key: "offer", label: "Offer", hint: "bold offer badge with your price" },
  { key: "crystal", label: "Crystal infused", hint: "ribbon badge + crystal zoom circle" },
  { key: "custom", label: "Custom", hint: "you describe the hero yourself" },
];
const MODEL_PREFS = ["No model shots", "Women", "Men", "Both"];
const TEXT_STYLES = [
  { key: "Normal", hint: "clean modern sans-serif, simple flat labels" },
  { key: "Minimal", hint: "thin elegant type, lots of whitespace, no boxes" },
  { key: "Luxury", hint: "refined serif + gold rules, editorial jewellery look" },
  { key: "Loud", hint: "big bold condensed headlines, high contrast" },
  { key: "Funny", hint: "playful hand-written accents, quirky callouts" },
  { key: "Offer", hint: "price-first: big offer badges, strike-through MRP" },
];

function Chips({ options, value, onPick }) {
  return (
    <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
      {options.map((o) => (
        <button key={o} type="button" onClick={() => onPick(o)}
          className={value === o ? "btn gold" : "btn ghost"}
          style={{ padding: "7px 16px", fontSize: 13 }}>
          {value === o ? "✓ " : ""}{o}
        </button>
      ))}
    </div>
  );
}

function Section({ n, title, children, show = true }) {
  if (!show) return null;
  return (
    <div className="card">
      <b style={{ fontSize: 15 }}><span style={{ color: "var(--gold-deep)" }}>{n} ·</span> {title}</b>
      <div style={{ marginTop: 10 }}>{children}</div>
    </div>
  );
}

export default function NewProduct() {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [category, setCategory] = useState("");
  const [finish, setFinish] = useState("");        // Gold Toned | Crystal Infused
  const [jtype, setJtype] = useState("");
  const [jtypeOther, setJtypeOther] = useState("");
  const [modelPref, setModelPref] = useState("Women");
  const [textStyle, setTextStyle] = useState("Normal");
  const [heroTypes, setHeroTypes] = useState(["normal"]);

  const isJewellery = category === "Gold Jewellery" || category === "Crystal Jewellery";
  const isCrystal = category === "Crystal Jewellery" || finish === "Crystal Infused"
    || category === "Crystal Home Decor";

  function toggleHero(key) {
    setHeroTypes((h) => h.includes(key) ? h.filter((k) => k !== key) : [...h, key]);
  }

  async function submit(e) {
    e.preventDefault();
    const f = e.target;
    if (!category) { setMsg("❌ Pick what you're listing (step 1)"); return; }
    if (!f.name.value.trim()) { setMsg("❌ Product name missing"); return; }
    setBusy(true);
    try {
      const inputs = { raw: [], crystal: [], refs: [], labels: [] };
      for (const kind of ["raw", "crystal", "refs", "labels"]) {
        const files = f[kind] ? [...f[kind].files] : [];
        for (let i = 0; i < files.length; i++) {
          setMsg(`Uploading ${kind} photo ${i + 1}/${files.length}…`);
          const safe = files[i].name.replace(/[^A-Za-z0-9._-]/g, "_") || `photo-${i}.jpg`;
          const blob = await upload(`jobs/_uploads/${kind}/${safe}`, files[i], {
            access: "public", handleUploadUrl: "/api/upload",
          });
          inputs[kind].push({ url: blob.url, name: safe });
        }
      }
      setMsg("Creating product…");
      const r = await fetch("/api/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: f.name.value, category, vendor: f.vendor.value,
          jewellery_finish: finish, jewellery_type: jtype === "Other" ? jtypeOther : jtype,
          price: f.price.value, mrp: f.mrp.value, sku: f.sku.value, variants: f.variants.value,
          hero_types: heroTypes,
          crystal_name: f.crystal_name?.value || "", crystal_benefit: f.crystal_benefit?.value || "",
          offer: f.offer?.value || "", hero_custom: f.hero_custom?.value || "",
          theme_mood: f.theme_mood.value, color_theme: f.color_theme.value,
          text_style: textStyle,
          background_style: f.background_style.value, model_pref: modelPref,
          details: f.details.value, notes: f.notes.value,
          ref_urls: f.ref_urls.value.split("\n").map((s) => s.trim()).filter(Boolean),
          inputs,
        }),
      });
      const job = await r.json();
      if (!r.ok) throw new Error(job.error || "failed");
      location.href = `/p/${job.slug}`;
    } catch (err) {
      setMsg(`❌ ${err.message}`);
      setBusy(false);
    }
  }

  return (
    <>
      <div className="eyebrow">New product</div>
      <h1>Create a listing</h1>
      <form onSubmit={submit}>
        <Section n={1} title="What are we listing?">
          <Chips options={LISTING_TYPES} value={category} onPick={setCategory} />
        </Section>

        <Section n={2} title="Jewellery details" show={isJewellery}>
          {category === "Gold Jewellery" && (
            <>
              <label>Finish</label>
              <Chips options={["Gold Toned", "Crystal Infused"]} value={finish} onPick={setFinish} />
            </>
          )}
          <label>Type of jewellery</label>
          <Chips options={JEWELLERY_TYPES} value={jtype} onPick={setJtype} />
          {jtype === "Other" && (
            <div><label>What is it?</label>
              <input type="text" value={jtypeOther} onChange={(e) => setJtypeOther(e.target.value)}
                placeholder="e.g. anklet, brooch, nose pin" /></div>
          )}
        </Section>

        <Section n={2.5} title="Crystal details" show={isCrystal}>
          <div className="grid2">
            <div><label>Infused crystal *</label><input type="text" name="crystal_name" placeholder="Pyrite" /></div>
            <div><label>Crystal benefit (short)</label><input type="text" name="crystal_benefit" placeholder="Brings Wisdom & Clarity" /></div>
          </div>
        </Section>

        <Section n={3} title="Basics">
          <div className="grid2">
            <div><label>Product name *</label><input type="text" name="name" required placeholder="e.g. Pyrite Turtle Pendant" /></div>
            <div><label>Vendor / brand *</label>
              <select name="vendor" defaultValue="Studd Muffyn">
                <option>Studd Muffyn</option>
                <option>Nuskhe By Paras</option>
                <option>studdmuffynlife</option>
                <option>Big Pucchi</option>
              </select>
            </div>
            <div><label>SKU</label><input type="text" name="sku" placeholder="NTN1234" /></div>
            <div><label>Selling price (₹)</label><input type="number" name="price" step="0.01" placeholder="699" /></div>
            <div><label>MRP / compare-at (₹)</label><input type="number" name="mrp" step="0.01" placeholder="999" /></div>
          </div>
          <label>Variants (free text)</label>
          <input type="text" name="variants" placeholder="Women 45cm ₹699 / Men 55cm ₹749" />
        </Section>

        <Section n={4} title="Hero image — pick one or more styles">
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            {HERO_TYPES.map((t) => (
              <button key={t.key} type="button" onClick={() => toggleHero(t.key)}
                className={heroTypes.includes(t.key) ? "btn gold" : "btn ghost"}
                style={{ padding: "7px 16px", fontSize: 13 }} title={t.hint}>
                {heroTypes.includes(t.key) ? "✓ " : ""}{t.label}
              </button>
            ))}
          </div>
          {heroTypes.includes("offer") && (
            <div><label>Offer &amp; price (rendered word-for-word)</label>
              <input type="text" name="offer" placeholder="PACK OF 2 · ₹499" /></div>
          )}
          {heroTypes.includes("custom") && (
            <div><label>Describe your custom hero</label>
              <textarea name="hero_custom" rows={2}
                placeholder="e.g. pendant on black velvet with rose petals, moody spotlight, text 'LIMITED DROP' top-right" /></div>
          )}
          <p className="mut small" style={{ marginBottom: 0 }}>One hero image per selected style.</p>
        </Section>

        <Section n={5} title="Look & feel">
          <div className="grid2">
            <div><label>Theme / mood</label>
              <input type="text" name="theme_mood" placeholder="airy premium · festive · minimal luxe · playful" /></div>
            <div><label>Color theme</label>
              <input type="text" name="color_theme" placeholder="white + gold · cream + gold · black + gold" /></div>
          </div>
          <label>Background / surface</label>
          <input type="text" name="background_style" placeholder="white silk drape · marble pedestal · slate stone" />
          <label>Text style — how words look on the images</label>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            {TEXT_STYLES.map((t) => (
              <button key={t.key} type="button" onClick={() => setTextStyle(t.key)}
                className={textStyle === t.key ? "btn gold" : "btn ghost"}
                style={{ padding: "7px 16px", fontSize: 13 }} title={t.hint}>
                {textStyle === t.key ? "✓ " : ""}{t.key}
              </button>
            ))}
          </div>
          <label>Model shots</label>
          <Chips options={MODEL_PREFS} value={modelPref} onPick={setModelPref} />
          <label>Inspo URLs (one per line — listings whose look you want)</label>
          <textarea name="ref_urls" rows={2} placeholder="https://studdmuffyn.com/products/..." />
        </Section>

        <Section n={6} title="Photos">
          <div className="grid2" style={{ gap: 16 }}>
            <div className="drop"><b>Raw product photos *</b><span>the actual product</span>
              <input type="file" name="raw" multiple accept="image/*" /></div>
            {isCrystal && (
              <div className="drop"><b>Crystal close-ups</b><span>macro shots — used for the zoom circle</span>
                <input type="file" name="crystal" multiple accept="image/*" /></div>
            )}
            <div className="drop"><b>Inspo screenshots</b><span>existing listings, competitor pages</span>
              <input type="file" name="refs" multiple accept="image/*" /></div>
            <div className="drop"><b>Label / packaging shots</b><span>ingredients, specs — becomes listing facts</span>
              <input type="file" name="labels" multiple accept="image/*" /></div>
          </div>
        </Section>

        <Section n={7} title="Details & brief">
          <label>Product details — everything the website content should say</label>
          <textarea name="details" rows={5}
            placeholder="Materials, sizes, weight, plating, benefits, what's in the box, care, target customer, occasion…" />
          <label>Notes for the writer (tone, USPs, what to avoid)</label>
          <textarea name="notes" rows={2} placeholder="e.g. focus on gifting angle, don't mention gold purity" />
          <p style={{ marginTop: 18 }}>
            <button className="btn gold" disabled={busy}>{busy ? "Working…" : "Create product"}</button>{" "}
            <a className="btn ghost" href="/">Cancel</a>{" "}
            <span className="mut small">{msg}</span>
          </p>
        </Section>
      </form>
    </>
  );
}
