import { allJobs, workerOnline, BUSY } from "../lib/store";
import RowPublish from "./row-publish";

export const dynamic = "force-dynamic";

function Row({ j }) {
  const t = new Date(j.updated || j.created).toLocaleString("en-IN", {
    day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit",
  });
  return (
    <tr>
      <td><a href={`/p/${j.slug}`}><b>{j.name}</b></a></td>
      <td>{j.category || "—"}</td>
      <td>₹{j.price || "—"}</td>
      <td><span className={`status ${j.status}`}>{j.status.replaceAll("_", " ")}</span></td>
      <td><RowPublish slug={j.slug} status={j.status} published={j.published} /></td>
      <td className="small mut">{t}</td>
    </tr>
  );
}

export default async function Home() {
  const [jobs, online] = await Promise.all([allJobs(), workerOnline()]);
  const done = jobs.filter((j) => j.status === "done").length;
  const busy = jobs.filter((j) => BUSY.includes(j.status)).length;
  const progress = jobs.filter((j) => ["new", "content_ready"].includes(j.status)).length;
  const activeNow = jobs.filter((j) => j.status.startsWith("generating_"));
  return (
    <>
      <div className="eyebrow">Dashboard</div>
      <h1>Products</h1>
      <p className="mut">Create a listing, watch its progress, and find every past listing here.</p>
      <div style={{
        background: online ? "#EAF0E8" : "#FBEAE7",
        border: `1px solid ${online ? "#C6D4C4" : "#EDC7C0"}`,
        borderRadius: 8, padding: "12px 18px", fontSize: 14,
      }}>
        {online ? "🟢 Generation engine: LIVE" : "🔴 Generation engine: OFFLINE — new jobs will wait in queue until it's back (Pulkit's Mac must be on and awake)"}
        {online && activeNow.length > 0 && (
          <span className="mut"> · working on: {activeNow.map((j) => j.name).join(", ")}</span>
        )}
        {online && activeNow.length === 0 && <span className="mut"> · idle, ready for jobs</span>}
      </div>
      <div className="grid4" style={{ margin: "18px 0" }}>
        <div className="stat"><b>{jobs.length}</b><span>total listings</span></div>
        <div className="stat"><b>{done}</b><span>completed</span></div>
        <div className="stat"><b>{progress}</b><span>in progress</span></div>
        <div className="stat"><b>{busy}</b><span>generating now</span></div>
      </div>
      <p>
        <a className="btn gold" href="/new">+ New product</a>{" "}
        <a className="btn ghost" href="/listings">All listings</a>{" "}
        <a className="btn ghost" href="/guide">Rules &amp; guide</a>
      </p>
      <div className="card">
        <h3 style={{ marginTop: 0 }}>Recent</h3>
        {jobs.length ? (
          <div className="tablewrap">
            <table>
              <thead><tr><th>Product</th><th>Category</th><th>Price</th><th>Status</th><th>Shopify</th><th>Updated</th></tr></thead>
              <tbody>{jobs.slice(0, 6).map((j) => <Row key={j.slug} j={j} />)}</tbody>
            </table>
          </div>
        ) : (
          <p className="mut">Nothing yet — create the first product.</p>
        )}
      </div>
    </>
  );
}
