/**
 * Extract a human-readable message from a backend error response.
 *
 * The generated API client returns `{ error }` on failure (it does not throw),
 * and FastAPI shapes that error as `{ detail: string }`, `{ detail:
 * [{ msg, loc, ... }] }`, or backend validation arrays like `{ detail:
 * [{ model, message }] }`. This normalizes those to a single string so it can
 * be rendered or thrown directly.
 */
export function detailFromError(err: unknown, fallback = "Request failed"): string {
    if (typeof err === "string") return err;
    const e = err as { detail?: unknown };
    if (typeof e?.detail === "string") return e.detail;
    if (Array.isArray(e?.detail) && e.detail.length > 0) {
        const messages = e.detail
            .map((item) => {
                if (typeof item === "string") return item;
                if (!item || typeof item !== "object") return null;
                const detail = item as {
                    message?: unknown;
                    msg?: unknown;
                    model?: unknown;
                    loc?: unknown;
                };
                const rawMessage = typeof detail.message === "string"
                    ? detail.message
                    : typeof detail.msg === "string"
                        ? detail.msg
                        : null;
                if (!rawMessage) return null;
                // Pydantic prefixes custom validator messages with "Value error, ".
                const message = rawMessage.replace(/^Value error, /, "");
                if (typeof detail.model === "string" && detail.model) {
                    return `${detail.model}: ${message}`;
                }
                // FastAPI request validation: name the offending field.
                const field = Array.isArray(detail.loc)
                    ? [...detail.loc].reverse().find((part) => typeof part === "string")
                    : undefined;
                return field && field !== "body" ? `${field}: ${message}` : message;
            })
            .filter((message): message is string => Boolean(message));
        if (messages.length > 0) return messages.join("\n");
    }
    return fallback;
}
