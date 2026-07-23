"use client";
import { useState } from "react";

const STORES = [
  { key: "SM", label: "Studd Muffyn", ready: true },
  { key: "SML", label: "SM Life", ready: true },
  { key: "NBP", label: "Nuskhe by Paras", ready: true },
];

export default function PublishPanel({ slug, published, imagesReady }) {
  const [working, setWorking] = useState("");
  const [msg, setMsg] = useState("");

  async function publish(store) {
    if (!confirm(`Post this listing to ${store} as a DRAFT product?`)) return;
    setWorking(store);
    setMsg("Creating product on Shopify…");
    try {
      const r = await fetch(`/api/jobs/${slug}/publish`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ store }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || "failed");
      location.reload();
    } catch (e) {
      setMsg(`❌ ${e.message}`);
      setWorking("");
    }
  }

  return (
    <div className="card">
      <h3 style={{ marginTop: 0 }}>Post to Shopify</h3>
      <p className="mut small" style={{ marginTop: -6 }}>
        Creates the product as a <b>Draft</b> on the chosen store — full copy, variants, prices,
        tags, SEO and all generated images. You press Publish inside Shopify after a final look.
        {!imagesReady && " Tip: generate the images first so they go along."}
      </p>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        {STORES.map((s) => {
          const pub = published?.[s.key];
          if (pub) {
            return (
              <a key={s.key} className="btn ghost" style={{ padding: "7px 16px", fontSize: 13 }}
                href={pub.admin_url} target="_blank" rel="noreferrer">
                ✓ On {s.label} — open draft ↗
              </a>
            );
          }
          return (
            <button key={s.key} className={s.ready ? "btn gold" : "btn ghost"}
              style={{ padding: "7px 16px", fontSize: 13, opacity: s.ready ? 1 : 0.45 }}
              disabled={!s.ready || !!working}
              title={s.ready ? "" : "store token pending"}
              onClick={() => publish(s.key)}>
              {working === s.key ? "Posting…" : `→ ${s.label}`}{!s.ready && " (soon)"}
            </button>
          );
        })}
        <span className="mut small">{msg}</span>
      </div>
    </div>
  );
}
