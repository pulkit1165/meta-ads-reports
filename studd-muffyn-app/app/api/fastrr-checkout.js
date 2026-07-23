// Vercel function: creates a Shiprocket (Fastrr) checkout access token for the
// app's cart. Keeps the API key + secret server-side — the app never sees them.
//
// POST /api/fastrr-checkout  { items: [{ variantId, qty }], }
//   → 200 { token, orderId, checkoutUrl }
//   → 503 { error } when keys aren't configured or Shiprocket is down
//     (the app then falls back to the Shopify permalink checkout)
//
// Env vars (Vercel project settings): FASTRR_API_KEY, FASTRR_API_SECRET
// Docs: "SRC Custom Integration" — POST /api/v1/access-token/checkout with
// X-Api-Key + X-Api-HMAC-SHA256 (base64 HMAC of the raw body, keyed by secret).

const crypto = require('crypto');

const FASTRR_BASE = 'https://checkout-api.shiprocket.com';
const APP_BASE = 'https://studd-muffyn-app.vercel.app';

module.exports = async (req, res) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  if (req.method === 'OPTIONS') return res.status(204).end();
  if (req.method !== 'POST') return res.status(405).json({ error: 'POST only' });

  const apiKey = process.env.FASTRR_API_KEY;
  const apiSecret = process.env.FASTRR_API_SECRET;
  if (!apiKey || !apiSecret) {
    return res.status(503).json({ error: 'fastrr-not-configured' });
  }

  try {
    const { items } = typeof req.body === 'string' ? JSON.parse(req.body) : req.body || {};
    if (!Array.isArray(items) || !items.length) {
      return res.status(400).json({ error: 'items required' });
    }

    const body = JSON.stringify({
      cart_data: {
        items: items.map((l) => ({ variant_id: String(l.variantId), quantity: Number(l.qty) || 1 })),
        custom_attributes: { source: 'studd_muffyn_app' },
        mobile_app: true,
      },
      redirect_url: `${APP_BASE}/order-success.html`,
      timestamp: new Date().toISOString(),
    });

    const hmac = crypto.createHmac('sha256', apiSecret).update(body).digest('base64');

    const r = await fetch(`${FASTRR_BASE}/api/v1/access-token/checkout`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Api-Key': apiKey,
        'X-Api-HMAC-SHA256': hmac,
      },
      body,
    });
    const j = await r.json().catch(() => ({}));
    const token = j && j.result && j.result.token;
    if (!r.ok || !token) {
      return res.status(503).json({ error: 'fastrr-token-failed', status: r.status, detail: j && j.error });
    }
    res.setHeader('Cache-Control', 'no-store');
    return res.status(200).json({
      token,
      orderId: j.result.data && j.result.data.order_id,
      expiresAt: j.result.expires_at,
      checkoutUrl: `${APP_BASE}/checkout.html?token=${encodeURIComponent(token)}`,
    });
  } catch (e) {
    return res.status(503).json({ error: String((e && e.message) || e) });
  }
};
