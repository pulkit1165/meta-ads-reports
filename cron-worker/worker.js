// Cloudflare Worker — pings GitHub to dispatch the today-live workflow
// at exactly :50 IST every hour. Replaces GHA's unreliable scheduled cron.
//
// Setup:
//   1. Create a fine-grained GitHub PAT with "Actions: Read & write" on
//      pulkit1165/meta-ads-reports.
//   2. Add it as the Worker secret GITHUB_PAT (set via wrangler secret put
//      or via the deploy workflow that streams it from a GH secret).
//
// Cron: configured in wrangler.toml — fires at UTC :20 hourly = IST :50.

const REPO  = 'pulkit1165/meta-ads-reports';
const FILE  = 'today-live.yml';
const REF   = 'main';

async function dispatchWorkflow(env) {
  const url = `https://api.github.com/repos/${REPO}/actions/workflows/${FILE}/dispatches`;
  const r = await fetch(url, {
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
  return r;
}

export default {
  // Native cron trigger — fires per the schedule in wrangler.toml.
  async scheduled(event, env, ctx) {
    const r = await dispatchWorkflow(env);
    const ts = new Date().toISOString();
    if (r.ok) {
      console.log(`[${ts}] dispatched ${FILE} — ok`);
    } else {
      const body = await r.text();
      console.error(`[${ts}] dispatch failed: ${r.status} ${body.slice(0, 300)}`);
    }
  },

  // Manual ping endpoint for testing — visit https://<worker>.workers.dev/ping
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === '/ping') {
      const r = await dispatchWorkflow(env);
      const body = await r.text();
      return new Response(
        `dispatch status: ${r.status}\n\n${body || '(empty body — usually means success on 204)'}`,
        { headers: { 'Content-Type': 'text/plain' } }
      );
    }
    if (url.pathname === '/') {
      return new Response(
        `meta-ads cron pinger\n\n` +
        `· /ping  - manually trigger the today-live workflow now\n` +
        `· cron   - automatic dispatch hourly at IST :50 (UTC :20)\n`,
        { headers: { 'Content-Type': 'text/plain' } }
      );
    }
    return new Response('not found', { status: 404 });
  },
};
