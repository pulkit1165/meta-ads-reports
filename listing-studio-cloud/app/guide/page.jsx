export default function Guide() {
  return (
    <>
      <div className="eyebrow">Team guide</div>
      <h1>Rules &amp; how it works</h1>

      {[
        ["1", "Collect material", <>
          <p><b>Raw photos</b> — the actual product, sharp, plain background, 2–4 angles. Every AI image is built from these.</p>
          <p><b>References / inspo</b> — screenshots of listings whose <i>look</i> you want. Controls style, not product.</p>
          <p><b>Label shots</b> — clear photo of label/box; its text becomes the only source of claims. No label (jewellery)? Skip.</p>
        </>],
        ["2", "Create the product", <>
          <p>+ New product → name, category, price, MRP, SKU, variants. <b>Product details</b> is everything the
          website should say; <b>Notes</b> is your brief to the writer. More detail = fewer retries.</p>
        </>],
        ["3", "Generate content (free)", <>
          <p>~2 minutes. Title, description, SEO, tags, collections, variants, FAQs + the 6-image plan.
          Read the claims carefully — words are free to fix, images are not.</p>
        </>],
        ["4", "Refine (free)", <>
          <p>Edit details/notes ("shorter, focus on gifting") and generate content again until it reads right.</p>
        </>],
        ["5", "Generate images (~2 credits each)", <>
          <p>~5–10 minutes for the 6-image sequence in the Studd Muffyn style. One bad frame → its own
          <b> Regenerate</b> button, don't re-run all six.</p>
        </>],
        ["6", "Final check", <>
          <p>Review on the product page. The package is saved automatically; posting to Shopify is a coming one-click step.</p>
        </>],
      ].map(([n, title, body]) => (
        <div className="step" key={n}>
          <div className="n">{n}</div>
          <div><h3>{title}</h3>{body}</div>
        </div>
      ))}

      <div className="card">
        <h3 style={{ marginTop: 0 }}>House rules</h3>
        <div className="tablewrap"><table>
          <thead><tr><th>Do</th><th>Don't</th></tr></thead>
          <tbody>
            <tr><td>Review content (free) before images</td><td>Generate images before copy is final</td></tr>
            <tr><td>Regenerate single images</td><td>Re-run all 6 for one bad frame</td></tr>
            <tr><td>Use real label photos for claims</td><td>Type ingredient claims from memory</td></tr>
            <tr><td>One product = one entry</td><td>Mix two products' photos in one job</td></tr>
          </tbody>
        </table></div>
        <h3>If something's stuck</h3>
        <p className="small">
          "Queued" for more than 2 minutes — Pulkit's MacBook (the generation engine) is off or asleep; the job
          starts automatically once it's back. "Generating…" — normal, the page refreshes itself.
          Red error chip — press the same button again; if it repeats, tell Pulkit.
        </p>
      </div>
    </>
  );
}
