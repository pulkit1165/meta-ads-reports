"""Listing Studio cloud worker — runs on Pulkit's Mac.

Polls the Vercel dashboard every 30 s for queued jobs, generates content and
images locally (claude -p + Higgsfield, same engine as the local studio), and
uploads results back to the cloud so the team sees them from anywhere.

Config: worker_config.json  {"base_url": "https://…vercel.app", "secret": "…"}
Run:    .venv/bin/python3 worker.py
"""
from __future__ import annotations

import base64
import json
import shutil
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pipeline  # reuse prompts + claude runner

STUDIO = Path(__file__).resolve().parent
CFG = json.loads((STUDIO / "worker_config.json").read_text())
BASE, SECRET = CFG["base_url"].rstrip("/"), CFG["secret"]
WORK_DIR = STUDIO / "worker_jobs"
pipeline.JOBS_DIR = WORK_DIR  # point the shared pipeline helpers at our dir

POLL_SECONDS = 10


def api(path: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json", "x-worker-secret": SECRET},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def report(slug: str, status: str, log: str, error: str = "") -> None:
    api("/api/worker/poll", {"update": {"slug": slug, "status": status, "log": log, "error": error}})


def upload(path: str, data: bytes, content_type: str) -> None:
    api("/api/worker/complete", {
        "path": path,
        "content_base64": base64.b64encode(data).decode(),
        "contentType": content_type,
    })


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as r:
        dest.write_bytes(r.read())


def prepare_local(job: dict) -> Path:
    """Recreate the job folder structure locally from cloud inputs."""
    jp = WORK_DIR / job["slug"]
    if jp.exists():
        shutil.rmtree(jp)
    (jp / "output" / "images").mkdir(parents=True)
    (jp / "job.json").write_text(json.dumps(job, indent=2, ensure_ascii=False))
    for kind in ("raw", "crystal", "refs", "labels"):
        for i, f in enumerate(job.get("inputs", {}).get(kind, [])):
            ext = Path(f.get("name", "img.jpg")).suffix or ".jpg"
            download(f["url"], jp / "inputs" / kind / f"{i:02d}{ext}")
    return jp


def do_content(job: dict) -> None:
    slug = job["slug"]
    jp = prepare_local(job)
    ok, msg = pipeline._run_claude(slug, pipeline.CONTENT_PROMPT.format(job_path=jp))
    listing = jp / "output" / "listing.json"
    if not listing.exists():
        time.sleep(20)  # one automatic retry on any failure
        ok, msg = pipeline._run_claude(slug, pipeline.CONTENT_PROMPT.format(job_path=jp))
    if listing.exists():
        upload(f"jobs/{slug}/output/listing.json", listing.read_bytes(), "application/json")
        report(slug, "content_ready", "content generated")
    else:
        report(slug, "error", f"content failed: {msg[:200]}", f"content: {msg[:300]}")


# Appended to EVERY image prompt in code — generation can never run without it.
PRODUCT_LOCK = (
    " PRODUCT LOCK (mandatory): reproduce the product EXACTLY as in the reference"
    " photo — identical design, pattern, weave/links, texture, clasp, stones, charms,"
    " engraving, thickness, proportions, color and finish."
    " NEGATIVE PROMPT — strictly avoid ALL of the following: redesigned product,"
    " altered shape, different clasp, added or removed elements, extra charms or"
    " pendants, changed bead count or size, color shift, thicker or thinner chain,"
    " different weave pattern, stylized or 'improved' product, fantasy version of"
    " the product. Only the scene around the product (background, surface, lighting,"
    " props, model, camera angle) may differ from the reference photo."
)


def do_images(job: dict, only_slots: list | None, listing_url: str | None) -> None:
    slug = job["slug"]
    jp = prepare_local(job)
    if listing_url:
        download(listing_url, jp / "output" / "listing.json")
        # bake the product lock into every prompt — deterministic, not agent-dependent
        lp = jp / "output" / "listing.json"
        try:
            lst = json.loads(lp.read_text())
            for p in lst.get("image_plan", []):
                if PRODUCT_LOCK not in (p.get("prompt") or ""):
                    p["prompt"] = (p.get("prompt") or "") + PRODUCT_LOCK
            lp.write_text(json.dumps(lst, indent=2, ensure_ascii=False))
        except Exception as e:
            print(f"{slug}: prompt-lock injection failed: {e}")
    if not (jp / "output" / "listing.json").exists():
        report(slug, "error", "images failed: listing.json missing", "images: listing.json missing — run content first")
        return
    only = f"only slot number(s) {only_slots}" if only_slots else "every slot in image_plan"
    # sonnet: the images phase is mechanical tool orchestration — a faster model
    # cuts several minutes of agent overhead without quality loss
    ok, msg = pipeline._run_claude(slug, pipeline.IMAGES_PROMPT.format(job_path=jp, only_clause=only), model="sonnet")
    pngs = sorted((jp / "output" / "images").glob("*.png"))
    if not pngs:
        time.sleep(30)  # one automatic retry on any failure
        ok, msg = pipeline._run_claude(slug, pipeline.IMAGES_PROMPT.format(job_path=jp, only_clause=only), model="sonnet")
        pngs = sorted((jp / "output" / "images").glob("*.png"))
    if pngs:
        for p in pngs:
            upload(f"jobs/{slug}/output/images/{p.name}", p.read_bytes(), "image/png")
        report(slug, "done", f"{len(pngs)} images uploaded")
    else:
        report(slug, "error", f"images failed: {msg[:200]}", f"images: {msg[:300]}")


MAX_PARALLEL = 8  # products generated simultaneously (EC2 has 4 GB swap as headroom)
_pool = ThreadPoolExecutor(max_workers=MAX_PARALLEL)
_inflight: set[str] = set()


def _handle(item: dict) -> None:
    job, phase = item["job"], item["phase"]
    slug = job["slug"]
    try:
        print(f"→ {slug} · {phase} (parallel {len(_inflight)}/{MAX_PARALLEL})")
        if phase == "content":
            do_content(job)
        else:
            do_images(job, item.get("only_slots"), item.get("listing_url"))
    except Exception as e:
        print(f"{slug} failed:", e)
        try:
            report(slug, "error", f"worker exception: {e}", str(e)[:200])
        except Exception:
            pass
    finally:
        _inflight.discard(slug)


def main() -> None:
    print(f"worker polling {BASE} every {POLL_SECONDS}s, {MAX_PARALLEL} jobs in parallel")
    consecutive_errors = 0
    while True:
        try:
            res = api("/api/worker/poll")
            consecutive_errors = 0
            for item in res.get("work", []):
                slug = item["job"]["slug"]
                if slug in _inflight:
                    continue
                _inflight.add(slug)
                _pool.submit(_handle, item)
        except Exception as e:
            print("poll error:", e)
            consecutive_errors += 1
            # a launchd-started process can wedge its DNS state if it came up
            # before the network did — exit after sustained failure and let
            # launchd (KeepAlive) relaunch us with a fresh resolver
            if consecutive_errors >= 10 and not _inflight:
                print(f"{consecutive_errors} consecutive poll errors — exiting for launchd restart")
                raise SystemExit(1)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
