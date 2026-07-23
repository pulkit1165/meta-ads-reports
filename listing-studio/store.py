"""SQLite index of all listings — the "past listings" database.

Source of truth stays the per-job folders (jobs/<slug>/); this DB is the fast
queryable index the dashboard reads. It is rebuilt from disk on startup and
upserted on every job write, so deleting listings.db is always safe.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

STUDIO_DIR = Path(__file__).resolve().parent
DB_PATH = STUDIO_DIR / "listings.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    slug      TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    category  TEXT,
    price     TEXT,
    mrp       TEXT,
    sku       TEXT,
    status    TEXT,
    error     TEXT,
    title     TEXT,          -- generated listing title (once content exists)
    images    INTEGER DEFAULT 0,
    created   INTEGER,
    updated   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_listings_status ON listings(status);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def upsert(job: dict, job_path: Path) -> None:
    """Refresh one listing's row from its job dict + on-disk outputs."""
    title = None
    lst = job_path / "output" / "listing.json"
    if lst.exists():
        try:
            title = json.loads(lst.read_text()).get("title")
        except Exception:
            pass
    imgs_dir = job_path / "output" / "images"
    n_imgs = len(list(imgs_dir.glob("*.png"))) + len(list(imgs_dir.glob("*.webp"))) if imgs_dir.exists() else 0
    with _conn() as c:
        c.execute(
            """INSERT INTO listings (slug,name,category,price,mrp,sku,status,error,title,images,created,updated)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(slug) DO UPDATE SET
                 name=excluded.name, category=excluded.category, price=excluded.price,
                 mrp=excluded.mrp, sku=excluded.sku, status=excluded.status,
                 error=excluded.error, title=excluded.title, images=excluded.images,
                 updated=excluded.updated""",
            (job["slug"], job["name"], job.get("category"), str(job.get("price", "")),
             str(job.get("mrp", "")), job.get("sku"), job.get("status"), job.get("error", ""),
             title, n_imgs, job.get("created", 0), int(time.time())),
        )


def rebuild(jobs_dir: Path) -> int:
    """Scan jobs/ and refresh every row. Returns number of listings indexed."""
    n = 0
    if jobs_dir.exists():
        for p in jobs_dir.iterdir():
            jf = p / "job.json"
            if jf.exists():
                try:
                    upsert(json.loads(jf.read_text()), p)
                    n += 1
                except Exception:
                    pass
    return n


def all_listings(q: str = "", status: str = "") -> list[sqlite3.Row]:
    sql = "SELECT * FROM listings WHERE 1=1"
    args: list = []
    if q:
        sql += " AND (name LIKE ? OR sku LIKE ? OR title LIKE ? OR category LIKE ?)"
        args += [f"%{q}%"] * 4
    if status:
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY updated DESC"
    with _conn() as c:
        return c.execute(sql, args).fetchall()


def stats() -> dict:
    with _conn() as c:
        rows = c.execute("SELECT status, COUNT(*) n FROM listings GROUP BY status").fetchall()
        total = sum(r["n"] for r in rows)
        by = {r["status"]: r["n"] for r in rows}
    busy = by.get("generating_content", 0) + by.get("generating_images", 0)
    return {"total": total, "done": by.get("done", 0), "content_ready": by.get("content_ready", 0),
            "busy": busy, "error": by.get("error", 0), "new": by.get("new", 0)}
