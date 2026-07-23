import { getJob, jobOutputs, BUSY } from "../../../lib/store";
import Controls from "./ui";
import ImagesPanel from "./images";
import PublishPanel from "./publish";

export const dynamic = "force-dynamic";

export default async function Product({ params }) {
  const { slug } = await params;
  const job = await getJob(slug);
  if (!job) return <p>Not found. <a href="/">← back</a></p>;
  const busy = BUSY.includes(job.status);
  const { listing, images } = await jobOutputs(slug);
  const imgBySlot = {};
  for (const im of images) {
    const m = im.pathname.match(/slot(\d+)/);
    if (m) imgBySlot[Number(m[1])] = im.url;
  }
  return (
    <>
      {busy && <meta httpEquiv="refresh" content="8" />}
      <h1>{job.name} <span className={`status ${job.status}`}>{job.status.replaceAll("_", " ")}</span></h1>
      <p className="mut small">{job.category} · ₹{job.price || "—"} (MRP ₹{job.mrp || "—"}) · SKU {job.sku || "—"}</p>
      {job.error && <div className="card" style={{ borderColor: "#EDD3B3" }}><b style={{ color: "var(--err)" }}>Error:</b> {job.error}</div>}

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Inputs</h3>
        <div className="grid4">
          {["raw", "crystal", "refs", "labels"].map((k) => (
            <div key={k}>
              <b className="small">{k === "raw" ? "Raw photos" : k === "crystal" ? "Crystal close-ups" : k === "refs" ? "References" : "Labels"}</b>
              <div className="thumbs">
                {(job.inputs?.[k] || []).map((f, i) => <img key={i} src={f.url} alt={f.name} />)}
              </div>
              {!(job.inputs?.[k] || []).length && <p className="small mut">none</p>}
            </div>
          ))}
        </div>
      </div>

      <Controls job={job} busy={busy} hasListing={!!listing} />

      {listing && (
        <>
          <div className="card">
            <h2 style={{ marginTop: 0 }}>Listing content</h2>
            <div className="tablewrap"><table><tbody>
              <tr><td className="mut small" style={{ width: 180 }}>Title</td><td><b>{listing.title}</b></td></tr>
              <tr><td className="mut small">Handle</td><td>{listing.handle}</td></tr>
              <tr><td className="mut small">Type / Vendor</td><td>{listing.product_type} · {listing.vendor}</td></tr>
              <tr><td className="mut small">Short description</td><td>{listing.short_description}</td></tr>
              <tr><td className="mut small">SEO title</td><td>{listing.seo_title}</td></tr>
              <tr><td className="mut small">SEO description</td><td>{listing.seo_description}</td></tr>
            </tbody></table></div>
            <h3>Description</h3>
            <div className="descbox" dangerouslySetInnerHTML={{ __html: listing.body_html || "" }} />
            <h3>Variants</h3>
            <div className="tablewrap"><table>
              <thead><tr><th>Variant</th><th>SKU</th><th>Price</th><th>Compare-at</th><th>Weight</th></tr></thead>
              <tbody>{(listing.variants || []).map((v, i) => (
                <tr key={i}><td>{v.title}</td><td>{v.sku}</td><td>₹{v.price}</td><td>₹{v.compare_at_price}</td><td>{v.grams} g</td></tr>
              ))}</tbody>
            </table></div>
            <h3>Collections</h3>
            <div>{(listing.collections || []).map((c) => <span className="chip" key={c}>{c}</span>)}</div>
            <h3>Tags</h3>
            <div>{(listing.tags || []).map((t) => <span className="chip" key={t}>{t}</span>)}</div>
            <h3>Metafields</h3>
            <div className="tablewrap"><table><tbody>
              {Object.entries(listing.metafields || {}).map(([k, v]) => (
                <tr key={k}><td className="mut small" style={{ width: 180 }}>{k.replaceAll("_", " ")}</td><td>{v}</td></tr>
              ))}
            </tbody></table></div>
            {listing.page_blocks && (
              <>
                <h3>Page blocks (accordion sections)</h3>
                <div className="tablewrap"><table><tbody>
                  {Object.entries(listing.page_blocks).map(([k, v]) => (
                    <tr key={k}><td className="mut small" style={{ width: 180 }}>{k.replaceAll("_", " ")}</td>
                      <td style={{ whiteSpace: "pre-wrap" }}>{Array.isArray(v) ? v.join("\n") : v}</td></tr>
                  ))}
                </tbody></table></div>
              </>
            )}
            <h3>FAQs</h3>
            {(listing.faqs || []).map((f, i) => (
              <p key={i}><b>{f.q}</b><br /><span className="mut">{f.a}</span></p>
            ))}
            <p style={{ marginTop: 18 }}>
              <a className="btn ghost" href={`/api/jobs/${slug}/csv`}>⬇ Shopify CSV (manual backup)</a>{" "}
              <a className="btn ghost" href={`/api/jobs/${slug}/amazon`}>⬇ Amazon pack</a>
            </p>
          </div>

          <PublishPanel slug={slug} published={job.published} imagesReady={images.length > 0} />

          <ImagesPanel slug={slug} plans={listing.image_plan || []} imgBySlot={imgBySlot} busy={busy}
            jobStatus={job.status} onlySlots={job.only_slots || null}
            productName={job.name} category={job.category} />
        </>
      )}

      {job.log?.length > 0 && (
        <div className="card">
          <h3 style={{ marginTop: 0 }}>Activity log</h3>
          <pre style={{ background: "#211D18", color: "#D8D2C6", borderRadius: 8, padding: "14px 18px", fontSize: 12, overflowX: "auto", maxHeight: 260, overflowY: "auto" }}>
            {job.log.slice(-25).join("\n")}
          </pre>
        </div>
      )}
    </>
  );
}
