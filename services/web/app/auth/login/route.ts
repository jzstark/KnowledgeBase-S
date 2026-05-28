import { NextRequest, NextResponse } from "next/server";

const API_URL = process.env.API_URL || "http://api:8000";

export async function POST(req: NextRequest) {
  const body = await req.json();

  const apiRes = await fetch(`${API_URL}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  const data = await apiRes.json().catch(() => ({}));

  if (!apiRes.ok) {
    return NextResponse.json(data, { status: apiRes.status });
  }

  // Parse the JWT out of the API's Set-Cookie and re-set it via Next.js
  // so the browser reliably receives the cookie.
  const setCookie = apiRes.headers.get("set-cookie") ?? "";
  const tokenMatch = setCookie.match(/(?:^|;\s*)token=([^;]+)/);
  const token = tokenMatch?.[1];

  const response = NextResponse.json(data);
  if (token) {
    response.cookies.set({
      name: "token",
      value: token,
      httpOnly: true,
      sameSite: "lax",
      maxAge: 7 * 24 * 3600,
      path: "/",
    });
  }

  return response;
}
