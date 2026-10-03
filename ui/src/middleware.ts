import type { NextRequest } from 'next/server';
import { NextResponse } from 'next/server';

import { getServerBackendUrl } from '@/lib/apiClient';
import { BRAND_COOKIE, BRAND_QUERY_PARAM, isBrandKey } from '@/lib/brand';

const OSS_TOKEN_COOKIE = 'dograh_auth_token';

// Paths that don't require authentication in OSS mode
const PUBLIC_PATHS = ['/auth/login', '/auth/signup'];

let cachedAuthProvider: string | null = null;

async function fetchAuthProvider(): Promise<string> {
  if (cachedAuthProvider) {
    return cachedAuthProvider;
  }

  try {
    const backendUrl = getServerBackendUrl();
    const res = await fetch(`${backendUrl}/api/v1/health`);
    if (res.ok) {
      const data = await res.json();
      // Only cache a DEFINITIVE answer from the backend. Never cache a failure:
      // this is a module-scoped cache with no TTL, so a single early request
      // during container startup (before the api service is reachable) would
      // otherwise poison it to 'local' for the life of the worker — redirecting
      // every Stack user to the local /auth/login form even though the backend
      // reports `stack`.
      cachedAuthProvider = (data.auth_provider as string) || 'local';
      return cachedAuthProvider;
    }
  } catch {
    // Backend not reachable — fall through without caching so we retry next request.
  }

  // Provider unknown (backend unreachable). Return a non-'local' sentinel so the
  // middleware does NOT guard/redirect: assuming 'local' here would bounce Stack
  // users to /auth/login. Deliberately not cached — the next request retries.
  return 'unknown';
}

/**
 * Persist the portal brand (`?brand=` on the iframe src) in a cookie.
 *
 * The app's own "/" page and the login guard below both redirect, and a
 * redirect drops the query string, so the page that finally renders would not
 * know which portal embedded it. The inline script in app/layout.tsx reads this
 * cookie. `SameSite=None; Partitioned` so it is kept when the portal embedding
 * the frame is a different site; it carries a theme name and nothing else.
 */
function withBrandCookie(request: NextRequest, response: NextResponse): NextResponse {
  const brand = request.nextUrl.searchParams.get(BRAND_QUERY_PARAM);
  if (isBrandKey(brand) && request.cookies.get(BRAND_COOKIE)?.value !== brand) {
    response.cookies.set(BRAND_COOKIE, brand, {
      path: '/',
      maxAge: 60 * 60 * 24 * 365,
      sameSite: 'none',
      secure: true,
      partitioned: true,
    });
  }
  return response;
}

export async function middleware(request: NextRequest) {
  return withBrandCookie(request, await guard(request));
}

async function guard(request: NextRequest): Promise<NextResponse> {
  const authProvider = await fetchAuthProvider();

  // Only handle OSS mode
  if (authProvider !== 'local') {
    return NextResponse.next();
  }

  const token = request.cookies.get(OSS_TOKEN_COOKIE)?.value;
  const { pathname } = request.nextUrl;

  // Allow public paths without auth
  if (PUBLIC_PATHS.some((p) => pathname.startsWith(p))) {
    return NextResponse.next();
  }

  // If no token, redirect to login
  if (!token) {
    const loginUrl = new URL('/auth/login', request.url);
    return NextResponse.redirect(loginUrl);
  }

  return NextResponse.next();
}

// Configure which routes the middleware runs on
export const config = {
  matcher: [
    /*
     * Match all request paths except:
     * - api routes
     * - _next/static (static files)
     * - _next/image (image optimization files)
     * - favicon.ico (favicon file)
     * - public static assets (anything with a file extension, e.g. /dograh-logo.png)
     */
    '/((?!api|_next/static|_next/image|favicon.ico|.*\\.(?:png|jpe?g|gif|svg|webp|avif|ico|woff2?|ttf|otf)).*)',
  ],
};
