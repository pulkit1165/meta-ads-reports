// Vercel Edge Middleware — HTTP Basic Auth gate for the Antariksh dashboard.
// Mirrors the Cloudflare _worker.js gate so the reports stay password-protected
// on Vercel's free tier (which otherwise serves static files publicly).
// Shared login: ntnteam / saisha@123  (override via env DASH_USER / DASH_PASSWORD).
export const config = { matcher: '/(.*)' };

export default function middleware(request) {
  const USER = process.env.DASH_USER || 'ntnteam';
  const PASS = process.env.DASH_PASSWORD || 'saisha@123';
  const auth = request.headers.get('authorization') || '';
  if (auth.startsWith('Basic ')) {
    try {
      const decoded = atob(auth.slice(6));
      const i = decoded.indexOf(':');
      if (decoded.slice(0, i) === USER && decoded.slice(i + 1) === PASS) {
        return; // authorized → continue to the static asset
      }
    } catch (_) { /* fall through to 401 */ }
  }
  return new Response('NTN Analytics — password required', {
    status: 401,
    headers: {
      'WWW-Authenticate': 'Basic realm="NTN Analytics", charset="UTF-8"',
      'Content-Type': 'text/plain; charset=utf-8',
    },
  });
}
