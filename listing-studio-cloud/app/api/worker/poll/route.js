import { NextResponse } from "next/server";
import { list, put } from "@vercel/blob";
import { allJobs, saveJob } from "../../../../lib/store";

// Every worker poll refreshes this; the dashboard shows online/offline from it.
async function heartbeat() {
  await put("worker/heartbeat.json", JSON.stringify({ t: Date.now() }), {
    access: "public",
    contentType: "application/json",
    addRandomSuffix: false,
    allowOverwrite: true,
    cacheControlMaxAge: 0,
  });
}

// The Mac worker calls this every ~30 s with its secret. Queued jobs are
// handed over and flipped to generating_* so they aren't picked up twice.
export async function POST(req) {
  if (req.headers.get("x-worker-secret") !== process.env.WORKER_SECRET) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  await heartbeat();
  const body = await req.json().catch(() => ({}));

  // status report from the worker
  if (body.update) {
    const jobs = await allJobs();
    const job = jobs.find((j) => j.slug === body.update.slug);
    if (job) {
      job.status = body.update.status;
      job.error = body.update.error || "";
      if (body.update.log) job.log.push(`${new Date().toISOString()} ${body.update.log}`);
      await saveJob(job);
    }
    return NextResponse.json({ ok: true });
  }

  // hand out queued work
  const jobs = await allJobs();
  const picked = [];

  // watchdog: a job stuck in generating_* for >40 min means its run died
  // (Mac slept, worker restarted mid-job) — put it back in the queue
  const STALE_MS = 40 * 60 * 1000;
  for (const job of jobs) {
    if (
      (job.status === "generating_content" || job.status === "generating_images") &&
      Date.now() - (job.updated || 0) > STALE_MS
    ) {
      job.status = job.status === "generating_content" ? "queued_content" : "queued_images";
      job.log.push(`${new Date().toISOString()} watchdog: run went silent, re-queued`);
      await saveJob(job);
    }
  }

  for (const job of jobs) {
    if (job.status === "queued_content" || job.status === "queued_images") {
      const phase = job.status === "queued_content" ? "content" : "images";
      job.status = phase === "content" ? "generating_content" : "generating_images";
      job.log.push(`${new Date().toISOString()} worker picked up ${phase}`);
      await saveJob(job);
      let listing_url = null;
      if (phase === "images") {
        const { blobs } = await list({ prefix: `jobs/${job.slug}/output/listing.json` });
        listing_url = blobs[0]?.url || null;
      }
      picked.push({ job, phase, only_slots: job.only_slots || null, listing_url });
    }
  }
  return NextResponse.json({ work: picked });
}
