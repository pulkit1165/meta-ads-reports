// Vercel serverless function — manual "Refresh data" button.
// Triggers the v2-ingest GitHub Actions workflow (full Meta + Shopify ingest +
// rebuild + redeploy). The GitHub token stays server-side (Vercel env), never
// in the browser. The whole route sits behind the dashboard's Basic-Auth
// middleware, so only logged-in team members can hit it.
//
// Setup (one-time): in Vercel project → Settings → Environment Variables add
//   GH_DISPATCH_TOKEN = a GitHub token with actions:write (repo scope) on
//   pulkit1165/meta-ads-reports. Until set, the button reports it's not configured.
module.exports = async (req, res) => {
  if (req.method !== 'POST') {
    res.status(405).json({ ok: false, error: 'POST only' });
    return;
  }
  const token = process.env.GH_DISPATCH_TOKEN;
  if (!token) {
    res.status(503).json({ ok: false, error: 'GH_DISPATCH_TOKEN not configured' });
    return;
  }
  const repo = process.env.GH_REPO || 'pulkit1165/meta-ads-reports';
  const wf = process.env.GH_WORKFLOW || 'v2-ingest.yml';
  try {
    const r = await fetch(
      `https://api.github.com/repos/${repo}/actions/workflows/${wf}/dispatches`,
      {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${token}`,
          'Accept': 'application/vnd.github+json',
          'X-GitHub-Api-Version': '2022-11-28',
          'User-Agent': 'antariksh-dashboard',
        },
        body: JSON.stringify({ ref: 'main' }),
      }
    );
    if (r.status === 204) {
      res.status(200).json({ ok: true, message: 'workflow dispatched' });
    } else {
      const t = await r.text();
      res.status(502).json({ ok: false, error: `GitHub ${r.status}`, detail: t.slice(0, 200) });
    }
  } catch (e) {
    res.status(500).json({ ok: false, error: String(e && e.message || e) });
  }
};
