import { NextResponse, type NextRequest } from "next/server";

/**
 * Gates the dashboard pages behind the shared-password session cookie.
 *
 * This only stops an unauthenticated *page* load — the API enforces the same
 * cookie itself (api/index.py::_authorized), because middleware does not run in
 * front of Python functions. Both checks are needed; neither is sufficient.
 *
 * The signature is verified server-side by the API. Middleware runs on the Edge
 * runtime, so it does a cheap shape + expiry check here and lets the API do the
 * HMAC verification. A forged cookie gets past this redirect but not past the
 * API, so no data leaks.
 */
const COOKIE_NAME = "md_session";
const PUBLIC_PATHS = ["/login"];

function looksValid(token: string | undefined): boolean {
  if (!token || !token.includes(".")) return false;
  const [payload] = token.split(".");
  try {
    const pad = "=".repeat((4 - (payload.length % 4)) % 4);
    const exp = Number(atob((payload + pad).replace(/-/g, "+").replace(/_/g, "/")));
    return Number.isFinite(exp) && exp * 1000 > Date.now();
  } catch {
    return false;
  }
}

export function middleware(req: NextRequest) {
  // No password configured (local dev) => no gate, matching the API's
  // auth_disabled() behaviour so the two never disagree.
  if (!process.env.DASHBOARD_PASSWORD) return NextResponse.next();

  const { pathname } = req.nextUrl;
  if (PUBLIC_PATHS.some((p) => pathname.startsWith(p))) return NextResponse.next();

  if (looksValid(req.cookies.get(COOKIE_NAME)?.value)) return NextResponse.next();

  const url = req.nextUrl.clone();
  url.pathname = "/login";
  return NextResponse.redirect(url);
}

export const config = {
  // Everything except Next.js internals, static assets, and /api (the Python
  // function does its own auth and must return JSON 401s, not an HTML redirect).
  matcher: ["/((?!api|_next/static|_next/image|favicon.ico).*)"],
};
