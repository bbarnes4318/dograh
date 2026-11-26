// GENERATED — do not edit by hand.
//
// Regenerate with `npm run codegen` against the target Dograh backend.
// Source of truth: the backend's model-backed node-spec catalog served
// from `/api/v1/node-types`.


/**
 * Reports AI phone interviews to Hoppwhistle's AI Recruiting Studio
 *
 * LLM hint: Hoppwhistle Recruiting is a post-call reporting node for AI phone interviews that Hoppwhistle compiles and starts. It sends the finished call's transcript, recording link, steps visited, extracted variables and usage back to Hoppwhistle, signed. It does not take part in the conversation and must not be connected to other nodes. The workflow's protected steps are checked by Hoppwhistle before every call; edit recruiting workflows from the Studio, not here.
 */
export interface Hoppwhistle_recruiting {
    type: "hoppwhistle_recruiting";
    /**
     * Short identifier shown in the canvas and call logs.
     */
    name?: string;
    /**
     * When false, finished interviews are not reported (and Hoppwhistle will refuse to call).
     */
    recruiting_enabled?: boolean;
    /**
     * Base URL of the Hoppwhistle API.
     */
    recruiting_base_url: string;
    /**
     * Hoppwhistle's RECRUITING_VOICE_ENGINE_SECRET. Reports are HMAC-signed; the secret is never sent.
     */
    recruiting_signing_secret: string;
    /**
     * Set by Hoppwhistle when it publishes the workflow.
     */
    recruiting_contract_version?: string;
    /**
     * Set by Hoppwhistle when it publishes the workflow.
     */
    recruiting_framework_version?: string;
}

/** Factory — sets `type` for you so you don't repeat the discriminator. */
export function hoppwhistle_recruiting(input: Omit<Hoppwhistle_recruiting, "type">): Hoppwhistle_recruiting {
    return { type: "hoppwhistle_recruiting", ...input };
}
