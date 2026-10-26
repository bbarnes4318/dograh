// GENERATED — do not edit by hand.
//
// Regenerate with `npm run codegen` against the target Dograh backend.
// Source of truth: the backend's model-backed node-spec catalog served
// from `/api/v1/node-types`.


/**
 * Insurance CRM, licensing and routing for AI callbacks and qualification
 *
 * LLM hint: Hoppwhistle is a post-call reporting integration for workflows that Hoppwhistle starts (AI Callback Recovery, AI Lead Qualifier). It sends the finished call's outcome, transcript, recording link, transfer attempts and usage back to Hoppwhistle, signed. It does not participate in the conversation graph and should not be connected to other nodes. The conversation's own tools are the Hoppwhistle tools installed by POST /api/v1/integrations/hoppwhistle/install.
 */
export interface Hoppwhistle {
    type: "hoppwhistle";
    /**
     * Short identifier for this Hoppwhistle configuration.
     */
    name?: string;
    /**
     * When false, finished calls are not reported to Hoppwhistle.
     */
    hoppwhistle_enabled?: boolean;
    /**
     * Base URL of the Hoppwhistle API, e.g. https://agents.example.com.
     */
    hoppwhistle_base_url: string;
    /**
     * The shared secret (Hoppwhistle's DOGRAH_AI_AGENT_SECRET). Reports are signed with HMAC-SHA256; the secret itself is never sent.
     */
    hoppwhistle_signing_secret: string;
    /**
     * Optional: your all-in AI cost per minute, reported with each call so Hoppwhistle can show AI cost. Blank: Hoppwhistle estimates it.
     */
    hoppwhistle_cost_per_minute_cents?: number;
}

/** Factory — sets `type` for you so you don't repeat the discriminator. */
export function hoppwhistle(input: Omit<Hoppwhistle, "type">): Hoppwhistle {
    return { type: "hoppwhistle", ...input };
}
