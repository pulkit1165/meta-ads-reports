import { NextResponse } from "next/server";
import { getJob, saveJob, BUSY } from "../../../../../lib/store";

// Queue a generation phase; the Mac worker picks it up within ~30 s.
export async function POST(req, { params }) {
  const { slug } = await params;
  const job = await getJob(slug);
  if (!job) return NextResponse.json({ error: "not found" }, { status: 404 });
  const b = await req.json();

  // merge repeat clicks instead of failing: same-phase re-queue is idempotent,
  // extra image slots are added to the pending list
  if (job.status === "queued_content" && b.phase === "content") {
    return NextResponse.json(job);
  }
  if (job.status === "queued_images" && b.phase === "images") {
    const newSlots = Array.isArray(b.slots) && b.slots.length ? b.slots : null;
    job.only_slots = (job.only_slots && newSlots)
      ? [...new Set([...job.only_slots, ...newSlots])].sort((a, c) => a - c)
      : null; // either request wanted "all slots" → do all
    job.log.push(`${new Date().toISOString()} queue merged → slots ${job.only_slots || "all"}`);
    await saveJob(job);
    return NextResponse.json(job);
  }
  if (BUSY.includes(job.status)) {
    return NextResponse.json({ error: "already working" }, { status: 409 });
  }
  if (b.phase === "content") {
    job.status = "queued_content";
  } else if (b.phase === "images") {
    job.status = "queued_images";
    job.only_slots = Array.isArray(b.slots) && b.slots.length ? b.slots : null;
  } else {
    return NextResponse.json({ error: "bad phase" }, { status: 400 });
  }
  job.error = "";
  job.log.push(`${new Date().toISOString()} queued ${b.phase}${job.only_slots ? ` slots ${job.only_slots}` : ""}`);
  await saveJob(job);
  return NextResponse.json(job);
}
