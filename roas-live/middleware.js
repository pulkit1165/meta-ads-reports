// Vercel Edge Middleware — HTTP Basic Auth gate for the hourly ROAS page.
// Same shared login as the Antariksh dashboard, because this page exposes
// per-website revenue and spend and Vercel serves static files publicly.
// Login: ntnteam / saisha@123  (override via env DASH_USER / DASH_PASSWORD).
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
        return; // authorized → serve the page
      }
    } catch (_) { /* fall through to 401 */ }
  }
  return new Response('NTN ROAS — password required', {
    status: 401,
    headers: {
      'WWW-Authenticate': 'Basic realm="NTN ROAS", charset="UTF-8"',
      'Content-Type': 'text/plain; charset=utf-8',
    },
  });
}
