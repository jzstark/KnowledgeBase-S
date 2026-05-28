import { NextRequest, NextResponse } from "next/server";

const API_URL = process.env.API_URL || "http://api:8000";

export async function POST(req: NextRequest) {
  const token = req.cookies.get("token")?.value;

  if (token) {
    await fetch(`${API_URL}/api/auth/logout`, {
      method: "POST",
      headers: { Cookie: `token=${token}` },
    }).catch(() => {});
  }

  const response = NextResponse.json({ ok: true });
  response.cookies.set({
    name: "token",
    value: "",
    httpOnly: true,
    sameSite: "lax",
    maxAge: 0,
    path: "/",
  });
  return response;
}
