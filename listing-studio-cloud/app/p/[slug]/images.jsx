"use client";
import { useState, useEffect } from "react";
import { upload } from "@vercel/blob/client";

// ── Prompt generator ─────────────────────────────────────────────
// Each option carries a phrase; clicking chips and pressing "Write prompt"
// assembles a full, editable prompt covering every aspect of the shot.
const GROUPS = [
  { key: "vibe", label: "Vibe / style", opts: [
    ["Minimalistic", "clean minimalistic aesthetic with lots of negative space, uncluttered and calm"],
    ["Luxury", "high-end luxury feel, rich premium and refined, elegant and expensive-looking"],
    ["Bold & vibrant", "bold vibrant high-energy look, saturated colours, scroll-stopping"],
    ["Editorial", "editorial magazine-style composition, tasteful and artful"],
    ["Playful", "playful cheerful mood, lively and fun"],
    ["Festive", "festive celebratory Indian-festival mood, warm and joyful"],
    ["Natural / earthy", "natural earthy organic feel, calm and grounded"],
  ]},
  { key: "bg", label: "Background", opts: [
    ["White studio", "seamless bright white studio backdrop"],
    ["Cream silk", "soft cream silk fabric drape"],
    ["Marble", "polished marble surface"],
    ["Warm gradient", "smooth warm gradient backdrop"],
    ["Dark & moody", "dark moody backdrop with a single pool of light"],
    ["Lifestyle scene", "real lifestyle setting (vanity table, hands, or a styled home)"],
    ["Botanical props", "styled with botanical props, fresh leaves and petals"],
  ]},
  { key: "light", label: "Lighting", opts: [
    ["Soft daylight", "soft natural daylight with gentle soft shadows"],
    ["Studio softbox", "even studio softbox lighting, crisp and clean"],
    ["Dramatic", "dramatic directional lighting with strong contrast"],
    ["Golden hour", "warm golden-hour glow"],
    ["Bright & airy", "bright airy high-key lighting"],
  ]},
  { key: "shot", label: "Shot / angle", opts: [
    ["Hero centered", "hero shot with the product centered and large, razor-sharp focus"],
    ["3/4 angle", "three-quarter angle view showing depth and form"],
    ["Flat-lay", "top-down flat-lay composition"],
    ["Macro close-up", "extreme macro close-up of texture and craftsmanship detail"],
    ["On-model", "worn or used by a model, natural and aspirational"],
    ["In-hand scale", "held in a hand to show real scale"],
  ]},
  { key: "palette", label: "Colour palette", opts: [
    ["Cream + gold", "cream and gold palette"],
    ["Black + gold", "black and gold palette with premium contrast"],
    ["Pastel", "soft pastel palette"],
    ["Jewel tones", "rich jewel-tone palette"],
    ["Warm earthy", "warm earthy neutral palette"],
  ]},
  { key: "text", label: "Text on image", opts: [
    ["No text", "__NOTEXT__"],
    ["Benefit headline", "one bold benefit headline in big modern display type across the top"],
    ["Offer badge", "a bold offer badge/ribbon in a top corner"],
    ["Feature callouts", "3 short feature callout chips with thin leader lines pointing to the product"],
    ["Review cards", "three 5-star review cards for social proof"],
  ]},
  { key: "extras", label: "Extras (pick any)", multi: true, opts: [
    ["Anti-tarnish shine", "emphasise the anti-tarnish mirror shine"],
    ["Water splash", "a dynamic water-splash accent"],
    ["Ingredient props", "key ingredients arranged as story props"],
    ["Gift box", "premium gift box / packaging in frame"],
    ["Soft reflection", "a subtle soft reflection under the product"],
    ["Shallow depth", "shallow depth of field with a dreamy background blur"],
  ]},
];

function phraseFor(key, title) {
  const g = GROUPS.find((x) => x.key === key);
  const o = g && g.opts.find((op) => op[0] === title);
  return o ? o[1] : "";
}

function assemblePrompt(sel, ctx) {
  const p = [];
  const name = ctx.productName || "the product";
  p.push(`Premium Indian D2C product photo for "${name}"${ctx.category ? ` (${ctx.category})` : ""}, square 1:1.`);
  p.push("Use the real product exactly as shown in the attached reference photos — same design, finish and details, do not redesign or substitute it.");
  if (sel.vibe) p.push("Overall look: " + phraseFor("vibe", sel.vibe) + ".");
  if (sel.shot) p.push("Composition: " + phraseFor("shot", sel.shot) + ".");
  if (sel.bg) p.push("Background: " + phraseFor("bg", sel.bg) + ".");
  if (sel.light) p.push("Lighting: " + phraseFor("light", sel.light) + ".");
  if (sel.palette) p.push("Palette: " + phraseFor("palette", sel.palette) + ".");
  const extras = (sel.extras || []).map((t) => phraseFor("extras", t)).filter(Boolean);
  if (extras.length) p.push("Details: " + extras.join(", ") + ".");
  const customs = (sel.customs || []).filter(Boolean);
  if (customs.length) p.push("Also: " + customs.join(", ") + ".");
  if (sel.text === "No text") {
    p.push("No text or graphics on the image — a clean product-only frame.");
  } else if (sel.text) {
    const words = (sel.overlay || "").trim();
    const ts = sel.textStyle ? phraseFor("textstyle", sel.textStyle) : "";
    p.push("On-image text: " + phraseFor("text", sel.text) + (words ? ` reading "${words}"` : "") +
      (ts ? `, styled as ${ts}` : "") + " — crisp, correctly spelled and professionally designed.");
  }
  p.push("Studd Muffyn brand style: bright, premium, high detail, sharp focus, advertising quality.");
  return p.join(" ");
}

const TEXT_STYLES = [
  ["Bold white sans", "modern rounded extra-bold sans-serif (Poppins), pure white with a soft drop shadow"],
  ["Gold serif", "elegant high-contrast serif in gold foil"],
  ["Handwritten script", "flowing handwritten script accent"],
  ["Minimal thin", "thin minimal uppercase letter-spaced sans"],
  ["Pop on blob", "bold sans with the key phrase on a bright accent-colour blob"],
];
// text-style phrases live in their own list; expose them through phraseFor
GROUPS.push({ key: "textstyle", label: "__hidden__", opts: TEXT_STYLES });

function composeTheme(sel) {
  return [sel.bg && phraseFor("bg", sel.bg), sel.light && phraseFor("light", sel.light),
    sel.vibe && phraseFor("vibe", sel.vibe)].filter(Boolean).join(", ");
}

const CUSTOM_KEY = "ls_custom_opts_v1";
const CHIP = (on) => ({ padding: "3px 9px", fontSize: 11, borderRadius: 99, cursor: "pointer",
  border: `1px solid ${on ? "var(--gold-deep,#96772A)" : "#D9CDB0"}`,
  background: on ? "var(--gold-deep,#96772A)" : "#fff", color: on ? "#fff" : "#3a352a" });
const GROUP_LABEL = { padding: 0, fontSize: 10.5, textTransform: "uppercase", letterSpacing: ".06em", color: "var(--mut,#6E6A5E)", margin: "0 0 3px" };

function PromptBuilder({ ctx, onWrite }) {
  const [open, setOpen] = useState(false);
  const [sel, setSel] = useState({ extras: [], customs: [] });
  const [words, setWords] = useState(ctx.overlay || "");
  const [customLib, setCustomLib] = useState([]);   // designer's saved custom chips (localStorage)
  const [newCustom, setNewCustom] = useState("");
  const [top, setTop] = useState(null);             // top-rated prompts (lazy loaded)

  useEffect(() => {
    try { setCustomLib(JSON.parse(localStorage.getItem(CUSTOM_KEY) || "[]")); } catch { /* ignore */ }
  }, []);
  function saveLib(list) { setCustomLib(list); try { localStorage.setItem(CUSTOM_KEY, JSON.stringify(list)); } catch { /* ignore */ } }

  function toggle(key, title, multi) {
    setSel((s) => {
      if (multi) {
        const cur = s[key] || [];
        return { ...s, [key]: cur.includes(title) ? cur.filter((t) => t !== title) : [...cur, title] };
      }
      return { ...s, [key]: s[key] === title ? undefined : title };
    });
  }
  function addCustom() {
    const v = newCustom.trim();
    if (!v) return;
    if (!customLib.includes(v)) saveLib([...customLib, v]);
    setSel((s) => ({ ...s, customs: [...(s.customs || []), v].filter((x, i, a) => a.indexOf(x) === i) }));
    setNewCustom("");
  }
  async function loadTop() {
    setTop("loading");
    try {
      const r = await fetch(`/api/ratings?category=${encodeURIComponent(ctx.category || "")}`);
      setTop((await r.json()).top || []);
    } catch { setTop([]); }
  }
  function write() {
    const payload = {
      prompt: assemblePrompt({ ...sel, overlay: words }, ctx),
      theme: composeTheme(sel),
      overlay: sel.text && sel.text !== "No text" ? words : "",
      textStyle: sel.textstyle ? phraseFor("textstyle", sel.textstyle) : "",
    };
    onWrite(payload);
    setOpen(false);
  }

  if (!open) {
    return (
      <button type="button" className="btn ghost" style={{ padding: "4px 10px", fontSize: 11.5, marginTop: 10 }}
        onClick={() => setOpen(true)}>🪄 Prompt generator</button>
    );
  }
  const bodyGroups = GROUPS.filter((g) => g.key !== "textstyle");
  return (
    <div style={{ marginTop: 10, border: "1px solid var(--sand, #E7E2D6)", borderRadius: 8, padding: 10, background: "#FBFAF7" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
        <b style={{ fontSize: 12.5 }}>🪄 Prompt generator</b>
        <div style={{ display: "flex", gap: 10 }}>
          <button type="button" onClick={loadTop} style={{ border: "none", background: "none", cursor: "pointer", fontSize: 12, color: "var(--gold-deep,#96772A)" }}>⭐ best prompts</button>
          <button type="button" onClick={() => setOpen(false)} style={{ border: "none", background: "none", cursor: "pointer", fontSize: 12, color: "var(--mut,#6E6A5E)" }}>close</button>
        </div>
      </div>

      {top && (
        <div style={{ marginBottom: 8, border: "1px dashed #D9CDB0", borderRadius: 6, padding: 8, background: "#fff" }}>
          <div style={GROUP_LABEL}>Top-rated prompts — click to load</div>
          {top === "loading" ? <p className="mut small" style={{ margin: 0 }}>loading…</p>
            : top.length === 0 ? <p className="mut small" style={{ margin: 0 }}>no rated prompts yet — rate some images first</p>
            : top.map((t, i) => (
              <button key={i} type="button" onClick={() => { onWrite({ prompt: t.prompt, theme: t.theme, overlay: t.overlay, textStyle: t.textStyle }); setOpen(false); }}
                style={{ display: "block", width: "100%", textAlign: "left", cursor: "pointer", border: "1px solid #EEE6D3", borderRadius: 5, background: "#FBFAF7", padding: "5px 7px", margin: "4px 0", fontSize: 11 }}>
                <b style={{ color: "var(--gold-deep,#96772A)" }}>{t.rating}/10</b> · {t.prompt.slice(0, 90)}…
              </button>
            ))}
        </div>
      )}

      {bodyGroups.map((g) => (
        <div key={g.key} style={{ marginBottom: 7 }}>
          <div style={GROUP_LABEL}>{g.label}</div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 5 }}>
            {g.opts.map(([title]) => {
              const on = g.multi ? (sel[g.key] || []).includes(title) : sel[g.key] === title;
              return <button key={title} type="button" onClick={() => toggle(g.key, title, g.multi)} style={CHIP(on)}>{title}</button>;
            })}
          </div>
        </div>
      ))}

      {/* Text on image — exact words + font style */}
      {sel.text && sel.text !== "No text" && (
        <>
          <div style={{ marginBottom: 7 }}>
            <div style={GROUP_LABEL}>Text on photo — exact words</div>
            <input type="text" value={words} onChange={(e) => setWords(e.target.value)}
              placeholder="e.g. 18K Gold · Anti-Tarnish · Pack of 2" style={{ fontSize: 12 }} />
          </div>
          <div style={{ marginBottom: 7 }}>
            <div style={GROUP_LABEL}>Text style</div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 5 }}>
              {TEXT_STYLES.map(([title]) => (
                <button key={title} type="button" onClick={() => toggle("textstyle", title, false)} style={CHIP(sel.textstyle === title)}>{title}</button>
              ))}
            </div>
          </div>
        </>
      )}

      {/* Custom options the designer adds themselves (saved for next time) */}
      <div style={{ marginBottom: 7 }}>
        <div style={GROUP_LABEL}>Your custom options</div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 5, marginBottom: 5 }}>
          {customLib.map((c) => {
            const on = (sel.customs || []).includes(c);
            return (
              <span key={c} style={{ display: "inline-flex", alignItems: "center", gap: 3 }}>
                <button type="button" onClick={() => toggle("customs", c, true)} style={CHIP(on)}>{c}</button>
                <button type="button" title="remove from library" onClick={() => saveLib(customLib.filter((x) => x !== c))}
                  style={{ border: "none", background: "none", cursor: "pointer", color: "#B4A98C", fontSize: 12, padding: 0 }}>×</button>
              </span>
            );
          })}
          {customLib.length === 0 && <span className="mut small">none yet — add your own below</span>}
        </div>
        <div style={{ display: "flex", gap: 5 }}>
          <input type="text" value={newCustom} onChange={(e) => setNewCustom(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addCustom(); } }}
            placeholder="type a custom look, prop, angle… then Add" style={{ fontSize: 12, flex: 1 }} />
          <button type="button" className="btn ghost" style={{ padding: "4px 12px", fontSize: 12 }} onClick={addCustom}>Add</button>
        </div>
      </div>

      <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
        <button type="button" className="btn gold" style={{ padding: "5px 12px", fontSize: 12 }} onClick={write}>Write prompt ↓</button>
        <button type="button" className="btn ghost" style={{ padding: "5px 12px", fontSize: 12 }}
          onClick={() => { setSel({ extras: [], customs: [] }); setWords(""); }}>Clear picks</button>
      </div>
      <p className="mut small" style={{ margin: "6px 0 0" }}>Fills the Prompt, Theme, Text &amp; Text-style boxes below — read, edit anything, then press Generate.</p>
    </div>
  );
}

function Cell({ slug, plan, imgUrl, busy, slotState, productName, category }) {
  const [prompt, setPrompt] = useState(plan.prompt || "");
  const [theme, setTheme] = useState(plan.theme || "");
  const [overlay, setOverlay] = useState(plan.overlay_text || "");
  const [textStyle, setTextStyle] = useState(plan.text_style || "");
  const [working, setWorking] = useState(false);
  const [uploadMsg, setUploadMsg] = useState("");
  const [rating, setRating] = useState(plan.rating || 0);
  const [rateMsg, setRateMsg] = useState("");
  const dirty = prompt !== (plan.prompt || "") || theme !== (plan.theme || "")
    || overlay !== (plan.overlay_text || "") || textStyle !== (plan.text_style || "");
  const refs = plan.refs || [];

  // The prompt generator fills every relevant field at once.
  function applyBuilder(out) {
    if (out.prompt) setPrompt(out.prompt);
    if (out.theme) setTheme(out.theme);
    if (typeof out.overlay === "string" && out.overlay) setOverlay(out.overlay);
    if (out.textStyle) setTextStyle(out.textStyle);
  }

  async function rate(n) {
    setRating(n);
    setRateMsg("saving…");
    try {
      await fetch(`/api/ratings`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ slug, slot: plan.slot, rating: n, prompt, theme,
          overlay, textStyle, imageUrl: imgUrl, productName, category }),
      });
      setRateMsg(`rated ${n}/10 ✓`);
    } catch { setRateMsg("❌ could not save"); }
  }

  async function savePrompt() {
    await fetch(`/api/jobs/${slug}/plan`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "update", slot: plan.slot, prompt, theme, overlay_text: overlay, text_style: textStyle }),
    });
  }

  async function addRefs(kind, files) {
    if (!files.length) return;
    setWorking(true);
    try {
      const added = [];
      for (let i = 0; i < files.length; i++) {
        setUploadMsg(`uploading ${i + 1}/${files.length}…`);
        const safe = files[i].name.replace(/[^A-Za-z0-9._-]/g, "_") || `photo-${i}.jpg`;
        const blob = await upload(`jobs/${slug}/slot-refs/${plan.slot}/${kind}/${safe}`, files[i], {
          access: "public",
          handleUploadUrl: "/api/upload",
        });
        added.push({ url: blob.url, name: safe, kind });
      }
      setUploadMsg("saving…");
      const r = await fetch(`/api/jobs/${slug}/plan`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "update", slot: plan.slot, addRefs: added }),
      });
      if (!r.ok) throw new Error((await r.json()).error || `save failed (${r.status})`);
      location.reload();
    } catch (err) {
      setUploadMsg(`❌ ${err.message || err}`);
      setWorking(false);
    }
  }

  async function removeRef(url) {
    setWorking(true);
    await fetch(`/api/jobs/${slug}/plan`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "update", slot: plan.slot, removeRefUrls: [url] }),
    });
    location.reload();
  }

  async function generate() {
    setWorking(true);
    if (dirty) await savePrompt();
    await fetch(`/api/jobs/${slug}/queue`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ phase: "images", slots: [plan.slot] }),
    });
    location.reload();
  }

  async function saveOnly() {
    setWorking(true);
    await savePrompt();
    location.reload();
  }

  return (
    <div className={`cell${slotState ? " working" : ""}`}>
      {slotState && (
        <div style={{ marginBottom: 8 }}>
          <span className={`gen-badge ${slotState}`}>
            <span className="dot" />{slotState === "generating" ? "Generating…" : "Queued"}
          </span>
        </div>
      )}
      {imgUrl ? (
        <img src={imgUrl} alt={plan.alt} style={slotState ? { opacity: 0.55 } : undefined} />
      ) : (
        <div className="imgframe">
          <p className="mut small">{slotState ? (slotState === "generating" ? "creating this image…" : "waiting for its turn…") : "not generated yet"}</p>
        </div>
      )}
      {imgUrl && (
        <div style={{ marginTop: 6 }}>
          <div style={{ fontSize: 10.5, textTransform: "uppercase", letterSpacing: ".06em", color: "var(--mut,#6E6A5E)", marginBottom: 3 }}>
            Rate this image (helps the AI learn)
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 3, alignItems: "center" }}>
            {[1,2,3,4,5,6,7,8,9,10].map((n) => (
              <button key={n} type="button" onClick={() => rate(n)} title={`${n}/10`}
                style={{ width: 22, height: 22, fontSize: 11, cursor: "pointer", borderRadius: 5, padding: 0,
                  border: `1px solid ${n <= rating ? "var(--gold-deep,#96772A)" : "#D9CDB0"}`,
                  background: n <= rating ? "var(--gold-deep,#96772A)" : "#fff",
                  color: n <= rating ? "#fff" : "#8a8168", fontWeight: n === rating ? 700 : 400 }}>{n}</button>
            ))}
            {rateMsg && <span className="mut small" style={{ marginLeft: 4 }}>{rateMsg}</span>}
          </div>
        </div>
      )}
      <div className="cap">
        <b>{plan.slot} · {plan.role}</b><br />{plan.filename}<br /><i>{plan.alt}</i>
      </div>
      <label style={{ marginTop: 10 }}>References for this image</label>
      {refs.length > 0 && (
        <div className="thumbs" style={{ marginTop: 4 }}>
          {refs.map((r) => (
            <div key={r.url} style={{ position: "relative" }}>
              <img src={r.url} alt={r.name} title={`${r.kind}: ${r.name} (click × to remove)`} />
              <span style={{ position: "absolute", left: 2, bottom: 2, fontSize: 9, fontWeight: 700,
                background: r.kind === "raw" ? "var(--ink)" : "var(--gold-deep)", color: "#fff",
                borderRadius: 3, padding: "0 4px", textTransform: "uppercase" }}>{r.kind}</span>
              <button onClick={() => removeRef(r.url)} disabled={working}
                style={{ position: "absolute", right: -4, top: -4, width: 17, height: 17, lineHeight: "14px",
                  borderRadius: "50%", border: "1px solid var(--sand)", background: "#fff",
                  fontSize: 11, cursor: "pointer", padding: 0 }}>×</button>
            </div>
          ))}
        </div>
      )}
      <div style={{ display: "flex", gap: 6, marginTop: 6, flexWrap: "wrap", fontSize: 11.5 }}>
        <label className="btn ghost" style={{ padding: "4px 10px", fontSize: 11.5, cursor: "pointer", margin: 0 }}>
          + raw product photo
          <input type="file" multiple accept="image/*" style={{ display: "none" }}
            disabled={working} onChange={(e) => addRefs("raw", [...e.target.files])} />
        </label>
        <label className="btn ghost" style={{ padding: "4px 10px", fontSize: 11.5, cursor: "pointer", margin: 0 }}>
          + inspiration
          <input type="file" multiple accept="image/*" style={{ display: "none" }}
            disabled={working} onChange={(e) => addRefs("inspo", [...e.target.files])} />
        </label>
        <span className="mut">{uploadMsg}</span>
      </div>
      <label style={{ marginTop: 10 }}>Theme (background, lighting, mood)</label>
      <input type="text" value={theme} onChange={(e) => setTheme(e.target.value)}
        placeholder="e.g. white silk, soft daylight, cream + gold, airy premium" style={{ fontSize: 12.5 }} />
      <label>Text on the photo (exact words · leave empty = no text)</label>
      <input type="text" value={overlay} onChange={(e) => setOverlay(e.target.value)}
        placeholder="e.g. 18K Gold Tone | Anti-Tarnish | Pack of 2" style={{ fontSize: 12.5 }} />
      <label>Text style (font vibe, color, effect)</label>
      <input type="text" value={textStyle} onChange={(e) => setTextStyle(e.target.value)}
        placeholder="e.g. rounded extra-bold sans (Poppins), white with soft shadow, key phrase on yellow blob"
        style={{ fontSize: 12.5 }} />
      <PromptBuilder ctx={{ productName, category, overlay }} onWrite={applyBuilder} />
      <label style={{ marginTop: 10 }}>Prompt <span className="mut small">— read &amp; edit before generating</span></label>
      <textarea rows={5} value={prompt} onChange={(e) => setPrompt(e.target.value)}
        placeholder="Use the 🪄 Prompt generator above, or describe exactly what this image should look like…" style={{ fontSize: 12.5 }} />
      <div style={{ display: "flex", gap: 6, marginTop: 8, flexWrap: "wrap" }}>
        <button className="btn ghost" style={{ padding: "5px 12px", fontSize: 12 }}
          disabled={busy || working || !dirty} onClick={saveOnly}>Save changes</button>
        <button className="btn gold" style={{ padding: "5px 12px", fontSize: 12 }}
          disabled={busy || working} onClick={generate}>
          {imgUrl ? "Regenerate" : "Generate"} this
        </button>
        {imgUrl && (
          <a className="btn ghost" style={{ padding: "5px 12px", fontSize: 12 }}
            href={`${imgUrl}${imgUrl.includes("?") ? "&" : "?"}download=1`}>⬇ Download</a>
        )}
      </div>
    </div>
  );
}

function AddCell({ slug, busy }) {
  const [open, setOpen] = useState(false);
  const [role, setRole] = useState("");
  const [prompt, setPrompt] = useState("");
  const [working, setWorking] = useState(false);

  async function add() {
    if (!prompt.trim()) return;
    setWorking(true);
    await fetch(`/api/jobs/${slug}/plan`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "add", role, prompt }),
    });
    location.reload();
  }

  if (!open) {
    return (
      <div className="cell" style={{ display: "flex", alignItems: "center", justifyContent: "center", minHeight: 180, cursor: "pointer" }}
        onClick={() => setOpen(true)}>
        <div style={{ textAlign: "center", color: "var(--gold-deep)" }}>
          <div style={{ fontSize: 34, lineHeight: 1 }}>+</div>
          <b style={{ fontSize: 14 }}>Add image</b>
          <p className="mut small" style={{ margin: "4px 0 0" }}>your own prompt, any shot you want</p>
        </div>
      </div>
    );
  }
  return (
    <div className="cell">
      <b style={{ fontSize: 14 }}>New image</b>
      <label>What is it? (e.g. "gift box shot")</label>
      <input type="text" value={role} onChange={(e) => setRole(e.target.value)} placeholder="packaging shot" />
      <label>Prompt</label>
      <textarea rows={4} value={prompt} onChange={(e) => setPrompt(e.target.value)}
        placeholder="Describe the image — the real product photos are always used as reference…" style={{ fontSize: 12.5 }} />
      <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
        <button className="btn gold" style={{ padding: "5px 12px", fontSize: 12 }}
          disabled={busy || working || !prompt.trim()} onClick={add}>Add slot</button>
        <button className="btn ghost" style={{ padding: "5px 12px", fontSize: 12 }}
          disabled={working} onClick={() => setOpen(false)}>Cancel</button>
      </div>
      <p className="mut small" style={{ marginTop: 6 }}>Adding is free — generating it later costs ~2 credits.</p>
    </div>
  );
}

export default function ImagesPanel({ slug, plans, imgBySlot, busy, jobStatus, onlySlots, productName, category }) {
  const [working, setWorking] = useState(false);
  const missing = plans.filter((p) => !imgBySlot[p.slot]).map((p) => p.slot);
  const allDone = missing.length === 0;

  // per-slot live state: which slots the current queue/run covers
  function stateFor(slot) {
    if (jobStatus !== "queued_images" && jobStatus !== "generating_images") return null;
    if (onlySlots && !onlySlots.includes(slot)) return null;
    return jobStatus === "generating_images" ? "generating" : "queued";
  }

  async function generateAll() {
    if (allDone && !confirm(`Regenerate all ${plans.length} images? (~${plans.length * 2} credits)`)) return;
    setWorking(true);
    await fetch(`/api/jobs/${slug}/queue`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ phase: "images", slots: allDone ? [] : missing }),
    });
    location.reload();
  }

  return (
    <div className="card">
      <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <h2 style={{ margin: 0 }}>Images</h2>
        <button className="btn gold" style={{ padding: "8px 20px", fontSize: 14 }}
          disabled={busy || working} onClick={generateAll}>
          {allDone ? "Regenerate all images" : `Generate all images (${missing.length} to go)`}
        </button>
        {(jobStatus === "generating_images" || jobStatus === "queued_images") ? (
          <span className={`gen-badge ${jobStatus === "generating_images" ? "generating" : "queued"}`}>
            <span className="dot" />
            {jobStatus === "generating_images" ? "Images are being created — page updates itself" : "In queue — starts within moments"}
          </span>
        ) : (
          <span className="mut small">
            {allDone ? "all images done — regenerate everything or fix single frames below" : "one click makes every remaining image together"}
          </span>
        )}
      </div>
      <div className="imgs" style={{ marginTop: 16 }}>
        {plans.map((p) => (
          <Cell key={p.slot} slug={slug} plan={p} imgUrl={imgBySlot[p.slot]} busy={busy}
            slotState={stateFor(p.slot)} productName={productName} category={category} />
        ))}
        <AddCell slug={slug} busy={busy} />
      </div>
    </div>
  );
}
