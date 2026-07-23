import { NextResponse } from "next/server";
import { getJob, saveJob, jobOutputs } from "../../../../lib/store";

export async function GET(_req, { params }) {
  const { slug } = await params;
  const job = await getJob(slug);
  if (!job) return NextResponse.json({ error: "not found" }, { status: 404 });
  const out = await jobOutputs(slug);
  return NextResponse.json({ ...job, output: out });
}

// update details/notes/inputs
export async function POST(req, { params }) {
  const { slug } = await params;
  const job = await getJob(slug);
  if (!job) return NextResponse.json({ error: "not found" }, { status: 404 });
  const b = await req.json();
  if (typeof b.details === "string") job.details = b.details;
  if (typeof b.notes === "string") job.notes = b.notes;
  if (b.addInputs) {
    for (const kind of ["raw", "refs", "labels"]) {
      if (b.addInputs[kind]?.length) {
        job.inputs[kind] = [...(job.inputs[kind] || []), ...b.addInputs[kind]];
      }
    }
  }
  job.log.push(`${new Date().toISOString()} details updated`);
  await saveJob(job);
  return NextResponse.json(job);
}
