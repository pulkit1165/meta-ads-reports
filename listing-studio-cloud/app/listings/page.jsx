import { allJobs } from "../../lib/store";
import RowPublish from "../row-publish";

export const dynamic = "force-dynamic";

export default async function Listings({ searchParams }) {
  const sp = await searchParams;
  const q = (sp?.q || "").toLowerCase();
  const status = sp?.status || "";
  let jobs = await allJobs();
  if (q) {
    jobs = jobs.filter((j) =>
      [j.name, j.sku, j.category].filter(Boolean).some((v) => v.toLowerCase().includes(q))
    );
  }
  if (status) jobs = jobs.filter((j) => j.status === status);
  const statuses = ["", "new", "queued_content", "generating_content", "content_ready",
    "queued_images", "generating_images", "done", "error"];
  return (
    <>
      <div className="eyebrow">Database</div>
      <h1>All listings</h1>
      <form className="card" method="get" style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
        <input type="text" name="q" defaultValue={sp?.q || ""} placeholder="Search name, SKU, category…"
          style={{ flex: 1, minWidth: 220 }} />
        <select name="status" defaultValue={status} style={{ width: "auto" }}>
          {statuses.map((s) => <option key={s} value={s}>{s.replaceAll("_", " ") || "any status"}</option>)}
        </select>
        <button className="btn ghost">Filter</button>
      </form>
      <div className="card">
        {jobs.length ? (
          <div className="tablewrap">
            <table>
              <thead><tr><th>Product</th><th>Category</th><th>Price</th><th>SKU</th><th>Status</th><th>Shopify</th><th>Updated</th></tr></thead>
              <tbody>
                {jobs.map((j) => (
                  <tr key={j.slug}>
                    <td><a href={`/p/${j.slug}`}><b>{j.name}</b></a></td>
                    <td>{j.category || "—"}</td>
                    <td>₹{j.price || "—"}</td>
                    <td>{j.sku || "—"}</td>
                    <td><span className={`status ${j.status}`}>{j.status.replaceAll("_", " ")}</span></td>
                    <td><RowPublish slug={j.slug} status={j.status} published={j.published} /></td>
                    <td className="small mut">
                      {new Date(j.updated || j.created).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="mut">No listings match.</p>
        )}
      </div>
    </>
  );
}
