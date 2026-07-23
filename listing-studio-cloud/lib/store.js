import { list, put } from "@vercel/blob";

// All state lives in Vercel Blob:
//   jobs/<slug>/job.json                    — metadata + status
//   jobs/<slug>/inputs/<kind>/<file>        — team uploads (raw/refs/labels)
//   jobs/<slug>/output/listing.json         — generated content package
//   jobs/<slug>/output/images/slotN_*.png   — generated images
// The Mac worker reads/writes the same paths with the same token.

export async function readJson(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) return null;
  return r.json();
}

export async function getJob(slug) {
  const { blobs } = await list({ prefix: `jobs/${slug}/job.json` });
  if (!blobs.length) return null;
  return readJson(blobs[0].url);
}

// Blob `list` returns at most 1000 entries per call — with photos and generated
// images each job carries ~15+ files, so the store passed 1000 blobs and
// alphabetically-late jobs silently vanished from unpaginated listings.
// Always walk the cursor.
async function listAll(prefix) {
  let cursor;
  const blobs = [];
  do {
    const res = await list({ prefix, cursor, limit: 1000 });
    blobs.push(...res.blobs);
    cursor = res.cursor;
  } while (cursor);
  return blobs;
}

export async function saveJob(job) {
  job.updated = Date.now();
  await put(`jobs/${job.slug}/job.json`, JSON.stringify(job, null, 2), {
    access: "public",
    contentType: "application/json",
    addRandomSuffix: false,
    allowOverwrite: true,
    cacheControlMaxAge: 0,
  });
  return job;
}

export async function allJobs() {
  const metas = (await listAll("jobs/")).filter((b) =>
    b.pathname.endsWith("/job.json")
  );
  const jobs = (
    await Promise.all(metas.map((b) => readJson(b.url)))
  ).filter(Boolean);
  jobs.sort((a, b) => (b.updated || 0) - (a.updated || 0));
  return jobs;
}

export async function jobOutputs(slug) {
  const { blobs } = await list({ prefix: `jobs/${slug}/output/` });
  const listing = blobs.find((b) => b.pathname.endsWith("listing.json"));
  const images = blobs
    .filter((b) => b.pathname.includes("/output/images/"))
    .sort((a, b) => a.pathname.localeCompare(b.pathname));
  return {
    listing: listing ? await readJson(listing.url) : null,
    images: images.map((b) => ({ pathname: b.pathname, url: b.url })),
  };
}

export const BUSY = ["queued_content", "generating_content", "queued_images", "generating_images"];

// worker liveness: heartbeat blob refreshed on every worker poll (~30 s)
export async function workerOnline() {
  const { blobs } = await list({ prefix: "worker/heartbeat.json" });
  if (!blobs.length) return false;
  const hb = await readJson(blobs[0].url);
  return !!hb && Date.now() - hb.t < 2 * 60 * 1000;
}
