// GENERATED — do not edit by hand.
//
// Regenerate with `npm run codegen` against the target Dograh backend.
// Source of truth: the backend's model-backed node-spec catalog served
// from `/api/v1/node-types`.


/**
 * Report finished calls to the Holiday Voices store in Hoppwhistle
 *
 * LLM hint: Holiday Voices is a post-call reporting integration for the Holiday Voices retail store's agents (shopping, support, outbound sales, wholesale). After a call whose initial context carries hv_session_id/hv_session_token it posts the outcome, extracted facts, transfer attempts, recording/transcript links, latency and cost to Hoppwhistle with the store's engine-key credential. It is not part of the conversation graph and must not be connected to other nodes. The conversation's tools are installed by POST /api/v1/integrations/holiday-voices/install.
 */
export interface Holiday_voices {
    type: "holiday_voices";
    /**
     * Short identifier for this Holiday Voices configuration.
     */
    name?: string;
    /**
     * When false, finished calls are not reported to Holiday Voices.
     */
    holiday_voices_enabled?: boolean;
    /**
     * Base URL of the Hoppwhistle API, e.g. https://agents.example.com.
     */
    holiday_voices_base_url: string;
    /**
     * UUID of this organization's bearer credential holding the store's engine key (hvk_...). The same credential the Holiday Voices tools use; the key itself is never stored on the workflow.
     */
    holiday_voices_credential_uuid: string;
    /**
     * Which Holiday Voices agent this is: SHOPPING, SUPPORT, OUTBOUND_SALES or WHOLESALE.
     */
    holiday_voices_agent?: string;
    /**
     * Optional all-in AI cost per minute, so the store sees each call's cost. Blank: cost is reported as not known.
     */
    holiday_voices_cost_per_minute_cents?: number;
}

/** Factory — sets `type` for you so you don't repeat the discriminator. */
export function holiday_voices(input: Omit<Holiday_voices, "type">): Holiday_voices {
    return { type: "holiday_voices", ...input };
}
