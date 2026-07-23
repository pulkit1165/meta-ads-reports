import { NextResponse } from "next/server";

export async function POST(req) {
  const { password } = await req.json();
  if (password !== process.env.TEAM_PASSWORD) {
    return NextResponse.json({ ok: false }, { status: 401 });
  }
  const res = NextResponse.json({ ok: true });
  res.cookies.set("ls_auth", process.env.AUTH_SECRET, {
    httpOnly: true,
    secure: true,
    sameSite: "lax",
    maxAge: 60 * 60 * 24 * 90,
    path: "/",
  });
  return res;
}
