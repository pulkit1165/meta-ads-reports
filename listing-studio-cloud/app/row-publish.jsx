"use client";
import { useState } from "react";

const STORES = ["SM", "SML", "NBP"];

export default function RowPublish({ slug, status, published }) {
  const [store, setStore] = useState("SM");
  const [working, setWorking] = useState(false);
  const [msg, setMsg] = useState("");
  const ready = ["content_ready", "done"].includes(status) || published;

  async function go() {
    if (!confirm(`Upload "${slug}" to ${store} as a DRAFT product?`)) return;
    setWorking(true);
    setMsg("uploading…");
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
      setMsg(`❌ ${String(e.message).slice(0, 60)}`);
      setWorking(false);
    }
  }

  const done = STORES.filter((s) => published?.[s]);
  return (
    <span style={{ display: "inline-flex", gap: 5, alignItems: "center", whiteSpace: "nowrap" }}>
      {done.map((s) => (
        <a key={s} href={published[s].admin_url} target="_blank" rel="noreferrer"
          className="small" style={{ color: "var(--ok)", fontWeight: 700, textDecoration: "none" }}
          title={`open draft on ${s}`}>✓{s}</a>
      ))}
      {ready ? (
        <>
          <select value={store} onChange={(e) => setStore(e.target.value)}
            style={{ width: "auto", padding: "3px 6px", fontSize: 12 }}>
            {STORES.map((s) => <option key={s}>{s}</option>)}
          </select>
          <button className="btn gold" style={{ padding: "4px 10px", fontSize: 11.5 }}
            disabled={working} onClick={go}>
            {working ? "…" : "↑ Shopify"}
          </button>
          <span className="mut" style={{ fontSize: 11 }}>{msg}</span>
        </>
      ) : (
        <span className="mut small">—</span>
      )}
    </span>
  );
}
