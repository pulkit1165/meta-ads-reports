// Cloudflare Worker — pings GitHub to dispatch the ingest + deploy workflows
// on a reliable schedule. Replaces GHA's flaky scheduled cron (~70% on-time).
//
// Two workflows on staggered crons:
//   :15 UTC (IST :45) → v2-ingest.yml   — Meta+Shopify ingest, ~8 min
//   :35 UTC (IST :05) → today-live.yml  — pulls fresh DB, rebuilds, deploys
//
// Setup:
//   1. Create a fine-grained GitHub PAT with "Actions: Read & write" on
//      pulkit1165/meta-ads-reports.
//   2. Add it as Worker secret GITHUB_PAT (set via wrangler secret put,
//      or via the deploy workflow that streams it from a GH secret).

const REPO = 'pulkit1165/meta-ads-reports';
const REF  = 'main';

// Map of cron expression → workflow file to dispatch when that cron fires.
// Must match wrangler.toml exactly (Cloudflare passes event.cron verbatim).
const CRON_TO_WORKFLOW = {
  '15 4-14 * * *': 'v2-ingest.yml',
  '35 4-14 * * *': 'today-live.yml',
};

async function dispatchWorkflow(env, file) {
  const url = `https://api.github.com/repos/${REPO}/actions/workflows/${file}/dispatches`;
  return fetch(url, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${env.GITHUB_PAT}`,
      'Accept': 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
      'User-Agent': 'meta-ads-cron-pinger',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ ref: REF }),
  });
}

export default {
  // Native cron trigger — Cloudflare calls this once per registered cron.
  async scheduled(event, env, ctx) {
    const ts = new Date().toISOString();
    const file = CRON_TO_WORKFLOW[event.cron];
    if (!file) {
      console.error(`[${ts}] unknown cron "${event.cron}" — no workflow mapped`);
      return;
    }
    const r = await dispatchWorkflow(env, file);
    if (r.ok) {
      console.log(`[${ts}] cron "${event.cron}" → dispatched ${file} ok`);
    } else {
      const body = await r.text();
      console.error(`[${ts}] dispatch ${file} failed: ${r.status} ${body.slice(0, 300)}`);
    }
  },

  // Manual ping endpoints — visit https://<worker>.workers.dev/ping-{ingest|deploy}
  async fetch(request, env) {
    const url = new URL(request.url);
    // CORS headers — the dashboard at meta-ads-reports.pages.dev calls
    // /ping-* from a "Refresh now" button. * is fine here because the only
    // sensitive operation is dispatching workflows, which requires no input
    // from the client beyond the URL path.
    const cors = {
      'Access-Control-Allow-Origin':  '*',
      'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type',
      'Access-Control-Max-Age':       '86400',
    };
    if (request.method === 'OPTIONS') return new Response(null, { headers: cors });

    if (url.pathname === '/ping-ingest' || url.pathname === '/ping') {
      const r = await dispatchWorkflow(env, 'v2-ingest.yml');
      const body = await r.text();
      return new Response(
        `dispatch v2-ingest.yml status: ${r.status}\n\n${body || '(empty body — usually success on 204)'}`,
        { headers: { ...cors, 'Content-Type': 'text/plain' } }
      );
    }
    if (url.pathname === '/ping-deploy') {
      const r = await dispatchWorkflow(env, 'today-live.yml');
      const body = await r.text();
      return new Response(
        `dispatch today-live.yml status: ${r.status}\n\n${body || '(empty body — usually success on 204)'}`,
        { headers: { ...cors, 'Content-Type': 'text/plain' } }
      );
    }
    if (url.pathname === '/') {
      return new Response(
        `meta-ads cron pinger\n\n` +
        `cron schedule (UTC = IST):\n` +
        `  :15 (IST :45) → v2-ingest.yml   (Meta+Shopify ingest, ~8 min)\n` +
        `  :35 (IST :05) → today-live.yml  (rebuild + deploy)\n\n` +
        `manual triggers:\n` +
        `  · /ping-ingest  - dispatch v2-ingest now\n` +
        `  · /ping-deploy  - dispatch today-live now\n`,
        { headers: { ...cors, 'Content-Type': 'text/plain' } }
      );
    }
    return new Response('not found', { status: 404, headers: cors });
  },
};
