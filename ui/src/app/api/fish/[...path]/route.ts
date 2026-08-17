/**
 * Fish Audio voice management, cloning and preview.
 *
 * Backs the Voice Studio page (/voice-studio). Everything here proxies
 * api.fish.audio server-side so FISH_API_KEY never reaches the browser — the
 * key is account-wide (cloning, deletion, billed generation), so it must not be
 * shipped to a client where anyone with devtools can lift it.
 *
 * Every request is gated on a real Dograh session: the `dograh_auth_token`
 * cookie is verified against the backend's /auth/me before anything is
 * proxied. Presence of the cookie is not enough — a cookie can be forged, a
 * signature checked by the backend cannot.
 *
 * Ported from the Hopwhistle API's routes/fish.ts so the Studio can live
 * beside the agent builder it feeds. Voice ids produced here go into the
 * Dograh TTS config as `voice` (Fish calls it reference_id).
 */

import { NextRequest, NextResponse } from "next/server";

const FISH_API_BASE = process.env.FISH_API_BASE || "https://api.fish.audio";
const FISH_API_KEY = process.env.FISH_API_KEY || "";
const BACKEND_URL = process.env.BACKEND_URL || "http://api:8000";

// s2.1-pro-free is the same model as s2.1-pro at $0, without a
// time-to-first-audio guarantee. Correct default for the Studio, where nobody
// is waiting on a live phone call.
const DEFAULT_MODEL = process.env.FISH_DEFAULT_MODEL || "s2.1-pro-free";

const OSS_TOKEN_COOKIE = "dograh_auth_token";
const PREVIEW_MAX_CHARS = 1200;
const CLONE_MAX_BYTES = 25 * 1024 * 1024;
const REQUEST_TIMEOUT_MS = 60_000;

const ALLOWED_MODELS = ["s2.1-pro-free", "s2.1-pro", "s2-pro", "s1"] as const;
const ALLOWED_VISIBILITY = ["private", "unlist", "public"] as const;
const ALLOWED_LATENCY = ["balanced", "normal"] as const;

/** Filters forwarded to Fish's /model search. Anything else is dropped. */
const ALLOWED_QUERY = [
  "self",
  "page_size",
  "page_number",
  "title",
  "tag",
  "language",
  "title_language",
  "sort_by",
  "author_id",
] as const;

function jsonError(code: string, message: string, status: number) {
  return NextResponse.json({ error: { code, message } }, { status });
}

const unauthorized = () => jsonError("UNAUTHORIZED", "Not authenticated", 401);
const notConfigured = () =>
  jsonError(
    "FISH_NOT_CONFIGURED",
    "Fish Audio is not configured. Set FISH_API_KEY on the ui service.",
    503,
  );
const unreachable = () => jsonError("FISH_UNREACHABLE", "Could not reach Fish Audio", 502);

/**
 * Confirm the caller has a real Dograh session. Verified against the backend
 * rather than decoded here, so the UI container never needs the JWT secret.
 */
async function requireSession(request: NextRequest): Promise<boolean> {
  const token = request.cookies.get(OSS_TOKEN_COOKIE)?.value;
  if (!token) return false;
  try {
    const response = await fetch(`${BACKEND_URL}/api/v1/auth/me`, {
      headers: { Authorization: `Bearer ${token}` },
      cache: "no-store",
    });
    return response.ok;
  } catch {
    return false;
  }
}

async function fishFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    return await fetch(`${FISH_API_BASE}${path}`, {
      ...init,
      signal: controller.signal,
      cache: "no-store",
      headers: { Authorization: `Bearer ${FISH_API_KEY}`, ...(init.headers || {}) },
    });
  } finally {
    clearTimeout(timer);
  }
}

/** Surface Fish's own error text rather than a generic 500 — cloning fails for
 *  specific, fixable reasons (sample too short, unsupported codec) and the user
 *  needs to see which. A 401 from Fish means our key is wrong, which is not the
 *  caller's problem, so it is not passed through as a 401. */
async function passThrough(response: Response, fallback: string) {
  const body = await response.text().catch(() => "");
  return jsonError(
    "FISH_UPSTREAM_ERROR",
    body?.slice(0, 500) || fallback,
    response.status === 401 ? 502 : response.status,
  );
}

const clamp = (value: unknown, min: number, max: number, fallback: number) => {
  const n = Number(value);
  return Number.isFinite(n) ? Math.min(Math.max(n, min), max) : fallback;
};

/** Guard shared by every handler. Returns a response to send, or null to continue. */
async function preflight(request: NextRequest): Promise<NextResponse | null> {
  if (!(await requireSession(request))) return unauthorized();
  if (!FISH_API_KEY) return notConfigured();
  return null;
}

export async function GET(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const blocked = await preflight(request);
  if (blocked) return blocked;

  const { path } = await context.params;
  const [head, id] = path;

  // ------------------------------------------------------------------ status
  if (head === "status" && path.length === 1) {
    let credit: unknown = null;
    try {
      const response = await fishFetch("/wallet/self/api-credit");
      if (response.ok) credit = await response.json();
    } catch {
      /* credit is decoration — a failure here must not break the Studio */
    }
    return NextResponse.json({
      configured: true,
      defaultModel: DEFAULT_MODEL,
      models: ALLOWED_MODELS,
      credit,
    });
  }

  // ------------------------------------------------------------------ voices
  if (head === "voices" && path.length === 1) {
    const incoming = request.nextUrl.searchParams;
    const params = new URLSearchParams();
    params.set("self", incoming.get("self") === "false" ? "false" : "true");
    params.set("page_size", String(Math.min(Number(incoming.get("page_size")) || 24, 100)));
    params.set("page_number", String(Math.max(Number(incoming.get("page_number")) || 1, 1)));
    for (const key of ALLOWED_QUERY) {
      if (key === "self" || key === "page_size" || key === "page_number") continue;
      const value = incoming.get(key);
      if (value) params.set(key, value);
    }

    try {
      const response = await fishFetch(`/model?${params.toString()}`);
      if (!response.ok) return passThrough(response, "Could not list voices");
      return NextResponse.json(await response.json());
    } catch {
      return unreachable();
    }
  }

  if (head === "voices" && id) {
    try {
      const response = await fishFetch(`/model/${encodeURIComponent(id)}`);
      if (!response.ok) return passThrough(response, "Could not fetch voice");
      return NextResponse.json(await response.json());
    } catch {
      return unreachable();
    }
  }

  return jsonError("NOT_FOUND", "Unknown Fish path", 404);
}

export async function POST(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const blocked = await preflight(request);
  if (blocked) return blocked;

  const { path } = await context.params;
  const [head] = path;

  // ------------------------------------------------------------------- clone
  if (head === "voices" && path.length === 1) {
    let form: FormData;
    try {
      form = await request.formData();
    } catch {
      return jsonError("BAD_REQUEST", "Expected a multipart upload", 400);
    }

    const files = form.getAll("voices").filter((part): part is File => part instanceof File);
    if (files.length === 0) {
      return jsonError("SAMPLES_REQUIRED", "Attach at least one audio sample", 400);
    }
    const total = files.reduce((sum, file) => sum + file.size, 0);
    if (total > CLONE_MAX_BYTES) {
      return jsonError(
        "SAMPLES_TOO_LARGE",
        `Samples must total under ${Math.round(CLONE_MAX_BYTES / 1024 / 1024)}MB`,
        400,
      );
    }

    const title = String(form.get("title") || "").trim();
    if (!title) return jsonError("TITLE_REQUIRED", "Give the voice a name", 400);

    const outgoing = new FormData();
    outgoing.set("title", title);
    const description = String(form.get("description") || "").trim();
    if (description) outgoing.set("description", description);

    const requested = String(form.get("visibility") || "private");
    outgoing.set(
      "visibility",
      (ALLOWED_VISIBILITY as readonly string[]).includes(requested) ? requested : "private",
    );
    outgoing.set(
      "enhance_audio_quality",
      form.get("enhance_audio_quality") === "false" ? "false" : "true",
    );
    files.forEach((file) => outgoing.append("voices", file, file.name));
    form.getAll("texts").forEach((text) => {
      if (typeof text === "string" && text.trim()) outgoing.append("texts", text.trim());
    });

    try {
      const response = await fishFetch("/model", { method: "POST", body: outgoing });
      if (!response.ok) return passThrough(response, "Cloning failed");
      return NextResponse.json(await response.json());
    } catch {
      return unreachable();
    }
  }

  // ----------------------------------------------------------------- preview
  if (head === "preview" && path.length === 1) {
    const body = (await request.json().catch(() => ({}))) as Record<string, unknown>;
    const text = typeof body.text === "string" ? body.text.trim() : "";

    if (!text) return jsonError("TEXT_REQUIRED", "Type a script to preview", 400);
    if (text.length > PREVIEW_MAX_CHARS) {
      return jsonError(
        "TEXT_TOO_LONG",
        `Previews are capped at ${PREVIEW_MAX_CHARS} characters`,
        400,
      );
    }

    const model = (ALLOWED_MODELS as readonly string[]).includes(body.model as string)
      ? (body.model as string)
      : DEFAULT_MODEL;
    const latency = (ALLOWED_LATENCY as readonly string[]).includes(body.latency as string)
      ? (body.latency as string)
      : "balanced";

    const payload: Record<string, unknown> = {
      text,
      format: "mp3",
      mp3_bitrate: 128,
      latency,
      chunk_length: clamp(body.chunk_length, 100, 300, 200),
      normalize: body.normalize === false ? false : true,
      prosody: {
        speed: clamp(body.speed, 0.5, 2.0, 1.0),
        volume: clamp(body.volume, -20, 20, 0),
      },
    };
    if (typeof body.voice === "string" && body.voice.trim()) {
      payload.reference_id = body.voice.trim();
    }
    if (body.temperature !== undefined) payload.temperature = clamp(body.temperature, 0, 1, 0.7);
    if (body.top_p !== undefined) payload.top_p = clamp(body.top_p, 0, 1, 0.7);

    try {
      // Fish takes the model as a header, not a body field.
      const response = await fishFetch("/v1/tts", {
        method: "POST",
        headers: { "Content-Type": "application/json", model },
        body: JSON.stringify(payload),
      });
      if (!response.ok) return passThrough(response, "Preview generation failed");

      return new NextResponse(await response.arrayBuffer(), {
        headers: {
          "Content-Type": "audio/mpeg",
          "Cache-Control": "no-store",
          "X-Fish-Model": model,
        },
      });
    } catch {
      return unreachable();
    }
  }

  return jsonError("NOT_FOUND", "Unknown Fish path", 404);
}

export async function PATCH(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const blocked = await preflight(request);
  if (blocked) return blocked;

  const { path } = await context.params;
  const [head, id] = path;
  if (head !== "voices" || !id) return jsonError("NOT_FOUND", "Unknown Fish path", 404);

  const body = (await request.json().catch(() => ({}))) as Record<string, unknown>;
  const payload: Record<string, unknown> = {};
  if (typeof body.title === "string") payload.title = body.title;
  if (typeof body.description === "string") payload.description = body.description;
  if ((ALLOWED_VISIBILITY as readonly string[]).includes(body.visibility as string)) {
    payload.visibility = body.visibility;
  }
  if (Object.keys(payload).length === 0) {
    return jsonError("NOTHING_TO_UPDATE", "No updatable fields supplied", 400);
  }

  try {
    const response = await fishFetch(`/model/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!response.ok) return passThrough(response, "Could not update voice");
    return NextResponse.json({ ok: true });
  } catch {
    return unreachable();
  }
}

export async function DELETE(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
) {
  const blocked = await preflight(request);
  if (blocked) return blocked;

  const { path } = await context.params;
  const [head, id] = path;
  if (head !== "voices" || !id) return jsonError("NOT_FOUND", "Unknown Fish path", 404);

  try {
    const response = await fishFetch(`/model/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (!response.ok) return passThrough(response, "Could not delete voice");
    return NextResponse.json({ ok: true });
  } catch {
    return unreachable();
  }
}
