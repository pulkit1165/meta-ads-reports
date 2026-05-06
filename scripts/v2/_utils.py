"""
NTN v2 — shared utilities for ingestion scripts.

Centralizes: DB connection, env loading, rate-limit-aware HTTP, ingest_log
write helpers, account/portal config.
"""

import os
import sqlite3
import time
import requests
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB = REPO_ROOT / 'state' / 'ntn.db'
IST = ZoneInfo('Asia/Kolkata')

load_dotenv(REPO_ROOT / '.env')

GRAPH_API = 'https://graph.facebook.com/v19.0'

# ── Account inventory by portal ───────────────────────────────────────────
# (env_var_name, friendly_name) — env_var_name resolves at runtime.
PORTAL_ACCOUNTS = {
    'SM': [
        ('SM_FRAGRANCE_01',   'SM Fragrance 01'),
        ('SM_SKIN',           'SM Skin'),
        ('SM_HAIR',           'SM Hair'),
        ('SM_CRYSTALS',       'SM Crystals'),
        ('SM_PERFUME',        'SM Perfume'),
        ('SM_CREDIT_LINE_05', 'SM CL 05'),
        ('SM_CREDIT_LINE_06', 'SM CL 06'),
    ],
    'SML': [
        ('SML_SKIN',     'SML Skin'),
        ('SML_HAIR',     'SML Hair'),
        ('SML_CRYSTALS', 'SML Crystals'),
        ('SML_CL_06',    'SML CL 06'),
        ('SML_CL_07',    'SML CL 07'),
    ],
    'NBP': [
        ('NBP_SKIN',         'NBP Skin'),
        ('NBP_HAIR_PERFUME', 'NBP Hair/Perfume'),
        ('NBP_CRYSTALS',     'NBP Crystals'),
    ],
}

PORTAL_SHOPIFY = {
    'SM':  ('SHOPIFY_STORE_URL',     'SHOPIFY_ACCESS_TOKEN'),
    'SML': ('SHOPIFY_STORE_URL_SML', 'SHOPIFY_ACCESS_TOKEN_SML'),
    'NBP': ('SHOPIFY_STORE_URL_NBP', 'SHOPIFY_ACCESS_TOKEN_NBP'),
}


# ── DB ────────────────────────────────────────────────────────────────────
def db_connect(path: Path = DEFAULT_DB) -> sqlite3.Connection:
    """Open SQLite with WAL + foreign keys + timeout."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.execute('PRAGMA journal_mode = WAL')
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA synchronous = NORMAL')
    return conn


def now_iso(tz=IST) -> str:
    return datetime.now(tz).isoformat()


def safe_float(v, default=0.0):
    try:
        return float(str(v).replace(',', '').replace('₹', '').strip())
    except (ValueError, TypeError):
        return default


def safe_int(v, default=0):
    try:
        return int(float(str(v).replace(',', '').strip()))
    except (ValueError, TypeError):
        return default


# ── Meta API rate-limit-aware client ──────────────────────────────────────
META_RATE_LIMIT_CODES = {4, 17, 32, 80004, 613}
META_RATE_LIMIT_PHRASES = (
    'application request limit reached',
    'rate limit reached',
    'user request limit reached',
    'too many calls',
    'reduce the amount of data',
    '#80004',
)


class MetaRateLimitError(Exception):
    """Raised when Meta API rate limit can't be retried away."""


def _is_meta_rate_limit(err: dict) -> bool:
    """Detects rate-limit errors across both legacy code-based and modern
    message-based responses. Meta has at least 5 different rate-limit error
    codes plus free-form messages, so we check both."""
    if not isinstance(err, dict):
        return False
    code = err.get('code')
    sub = err.get('error_subcode')
    msg = (err.get('message') or err.get('error_user_msg') or '').lower()
    if code in META_RATE_LIMIT_CODES or sub in META_RATE_LIMIT_CODES:
        return True
    return any(p in msg for p in META_RATE_LIMIT_PHRASES)


def meta_get(url: str, params: dict = None, *, max_retries: int = 5,
             initial_backoff: int = 30, token: str = None) -> dict:
    """GET against Meta Graph API with exponential backoff on rate limits.
    Sleeps 30/60/120/240/480s on rate-limit hits.
    Raises MetaRateLimitError after max_retries.
    Returns the parsed JSON dict (whatever Meta returned)."""
    p = dict(params or {})
    p['access_token'] = token or os.getenv('META_ACCESS_TOKEN')
    backoff = initial_backoff
    last_err = None
    for attempt in range(max_retries):
        try:
            r = requests.get(url, params=p, timeout=60)
        except requests.exceptions.RequestException as e:
            last_err = str(e)
            print(f"   [meta_get] request error attempt {attempt+1}: {e}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 600)
            continue
        try:
            data = r.json()
        except ValueError:
            last_err = f'non-JSON response (HTTP {r.status_code})'
            print(f"   [meta_get] {last_err}")
            time.sleep(backoff); backoff = min(backoff * 2, 600); continue
        # Explicit error in body
        if 'error' in data:
            err = data['error']
            if _is_meta_rate_limit(err):
                print(f"   [meta_get] rate limit hit (attempt {attempt+1}): "
                      f"code={err.get('code')} sub={err.get('error_subcode')} "
                      f"msg={(err.get('message') or '')[:80]}")
                print(f"   [meta_get] sleeping {backoff}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 600)
                continue
            # Non-rate-limit error — don't loop, surface it
            last_err = err.get('message', 'unknown error')
            print(f"   [meta_get] non-retryable error: {last_err[:120]}")
            return data
        # HTTP-level rate limit fallback (rare for Meta but be safe)
        if r.status_code == 429:
            ra = int(r.headers.get('Retry-After', backoff))
            print(f"   [meta_get] HTTP 429, Retry-After={ra}s")
            time.sleep(ra)
            backoff = min(backoff * 2, 600)
            continue
        return data
    raise MetaRateLimitError(
        f"Meta API rate-limited after {max_retries} retries. last_err={last_err}"
    )


def meta_paginate(url: str, params: dict, *, max_pages: int = 50,
                  token: str = None) -> list:
    """Paginate through Meta's `data` field using `paging.next`. Inherits
    rate-limit handling from meta_get on every page request."""
    out = []
    next_url = url
    next_params = dict(params)
    for page in range(max_pages):
        data = meta_get(next_url, next_params, token=token)
        if 'error' in data:
            print(f"   [meta_paginate] page {page+1} error — stopping")
            break
        out.extend(data.get('data', []) or [])
        nxt = (data.get('paging') or {}).get('next')
        if not nxt:
            break
        next_url = nxt
        next_params = {}     # full URL has token + params baked in
    return out


# ── ingest_log helpers ────────────────────────────────────────────────────
def log_ingest_start(conn, job_name: str, target_date: str) -> str:
    started = now_iso()
    conn.execute(
        "INSERT INTO ingest_log(job_name, target_date, started_at, status) "
        "VALUES(?, ?, ?, 'running')",
        (job_name, target_date, started)
    )
    return started


def log_ingest_finish(conn, job_name: str, target_date: str, started: str,
                      status: str, rows_written: int = 0,
                      error_message: str = None):
    conn.execute(
        "UPDATE ingest_log SET finished_at=?, status=?, rows_written=?, error_message=? "
        "WHERE job_name=? AND target_date=? AND started_at=?",
        (now_iso(), status, rows_written, error_message,
         job_name, target_date, started)
    )
