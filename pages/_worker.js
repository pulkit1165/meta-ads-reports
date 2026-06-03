// Cloudflare Pages advanced-mode worker — puts a shared-password gate (HTTP
// Basic Auth) in front of the whole dashboard. Copied to out/_worker.js during
// each deploy (see the "auth gate" step in the deploy workflows).
//
// Shared login defaults to the username/password below, but each can be
// overridden WITHOUT a code change via Pages env vars DASH_USER / DASH_PASSWORD
// (Cloudflare → Pages → project → Settings → Environment variables → Production).
//
// Robust by design: any error falls through and serves the asset, so a bug can
// never lock the team out.
const DEFAULT_USER = 'ntnteam';
const DEFAULT_PASS = 'saisha@123';

export default {
  async fetch(request, env) {
    try {
      const USER = (env && env.DASH_USER) || DEFAULT_USER;
      const PASS = (env && env.DASH_PASSWORD) || DEFAULT_PASS;
      if (PASS) {
        const auth = request.headers.get('Authorization') || '';
        let ok = false;
        if (auth.startsWith('Basic ')) {
          try {
            const decoded = atob(auth.slice(6));          // "user:pass"
            const i = decoded.indexOf(':');
            ok = decoded.slice(0, i) === USER && decoded.slice(i + 1) === PASS;
          } catch (_) { ok = false; }
        }
        if (!ok) {
          return new Response('🔒 NTN Analytics — password required', {
            status: 401,
            headers: {
              'WWW-Authenticate': 'Basic realm="NTN Analytics", charset="UTF-8"',
              'Content-Type': 'text/plain; charset=utf-8',
            },
          });
        }
      }
    } catch (_) {
      // fall through — serve the asset rather than risk locking everyone out
    }
    return env.ASSETS.fetch(request);
  },
};
