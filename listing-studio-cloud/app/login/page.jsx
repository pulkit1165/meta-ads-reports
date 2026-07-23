"use client";
import { useState } from "react";

export default function Login() {
  const [pw, setPw] = useState("");
  const [err, setErr] = useState("");
  async function submit(e) {
    e.preventDefault();
    const r = await fetch("/api/auth", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: pw }),
    });
    if (r.ok) {
      const next = new URLSearchParams(location.search).get("next") || "/";
      location.href = next;
    } else setErr("Wrong password.");
  }
  return (
    <div style={{ maxWidth: 380, margin: "80px auto 0" }}>
      <div className="eyebrow">Studd Muffyn · Internal tool</div>
      <h1>Listing Studio</h1>
      <form className="card" onSubmit={submit}>
        <label>Team password</label>
        <input type="password" value={pw} onChange={(e) => setPw(e.target.value)} autoFocus />
        {err && <p style={{ color: "var(--err)" }}>{err}</p>}
        <p style={{ marginTop: 14 }}>
          <button className="btn gold">Enter</button>
        </p>
      </form>
    </div>
  );
}
