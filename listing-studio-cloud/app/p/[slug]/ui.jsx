"use client";
import { useState } from "react";

export default function Controls({ job, busy, hasListing }) {
  const [working, setWorking] = useState(false);
  const [details, setDetails] = useState(job.details || "");
  const [notes, setNotes] = useState(job.notes || "");

  async function queue(phase, slots) {
    setWorking(true);
    await fetch(`/api/jobs/${job.slug}/queue`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ phase, slots }),
    });
    location.reload();
  }

  async function saveDetails(e) {
    e.preventDefault();
    setWorking(true);
    await fetch(`/api/jobs/${job.slug}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ details, notes }),
    });
    location.reload();
  }

  return (
    <>
      <div className="card">
        <h3 style={{ marginTop: 0 }}>Product details &amp; brief</h3>
        <p className="small mut" style={{ marginTop: -6 }}>
          The content is written from this. Edit and press <b>1 · Generate content</b> again to rewrite.
        </p>
        <form onSubmit={saveDetails}>
          <label>Product details — everything the website content should say</label>
          <textarea rows={6} value={details} onChange={(e) => setDetails(e.target.value)} />
          <label>Notes for the writer (tone, USPs, what to avoid)</label>
          <textarea rows={3} value={notes} onChange={(e) => setNotes(e.target.value)} />
          <p><button className="btn ghost" disabled={working}>Save details</button></p>
        </form>
      </div>
      <div className="card">
        <h3 style={{ marginTop: 0 }}>Generate</h3>
        <button className="btn gold" disabled={busy || working} onClick={() => queue("content")}>
          {hasListing ? "Rewrite content" : "1 · Generate content"}
        </button>{" "}
        <span className="mut small">
          {busy ? "Working… page refreshes itself."
            : hasListing ? "Content ready — images are generated from the Images section below."
            : "Content first (free) — the Images section appears once it's ready."}
        </span>
      </div>
    </>
  );
}
