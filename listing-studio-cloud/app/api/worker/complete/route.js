import { NextResponse } from "next/server";
import { put } from "@vercel/blob";

// The Mac worker uploads generated files here one at a time
// (base64 in JSON keeps each request well under the serverless body limit).
export async function POST(req) {
  if (req.headers.get("x-worker-secret") !== process.env.WORKER_SECRET) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  const { path, content_base64, contentType } = await req.json();
  if (!path?.startsWith("jobs/") || !content_base64) {
    return NextResponse.json({ error: "bad payload" }, { status: 400 });
  }
  const buf = Buffer.from(content_base64, "base64");
  const blob = await put(path, buf, {
    access: "public",
    contentType: contentType || "application/octet-stream",
    addRandomSuffix: false,
    allowOverwrite: true,
    cacheControlMaxAge: 0,
  });
  return NextResponse.json({ ok: true, url: blob.url });
}
