/**
 * Voice Studio catalog.
 *
 * The set of voices an operator can assign to an AI voice agent. This is a
 * static catalog (names, characteristics, sample copy) — not live telemetry.
 * `previewUrl` is optional: cards render a play control only when a preview
 * asset is actually configured, so we never imply audio that isn't there.
 */

export type VoiceTone = "warm" | "authoritative" | "energetic" | "calm";

export interface StudioVoice {
    id: string;
    name: string;
    /** Short persona line shown under the name. */
    descriptor: string;
    accent: string;
    tone: VoiceTone;
    /** Typical deployment this voice suits. */
    bestFor: string;
    /** Optional preview asset served from /public. */
    previewUrl?: string;
}

export const VOICE_CATALOG: StudioVoice[] = [
    {
        id: "nova",
        name: "Nova",
        descriptor: "Warm, unhurried, builds trust fast",
        accent: "US — General American",
        tone: "warm",
        bestFor: "Inbound qualification",
    },
    {
        id: "atlas",
        name: "Atlas",
        descriptor: "Measured and credible, low register",
        accent: "US — Midwest",
        tone: "authoritative",
        bestFor: "Compliance and verification",
    },
    {
        id: "ember",
        name: "Ember",
        descriptor: "Bright and quick, keeps momentum",
        accent: "US — West Coast",
        tone: "energetic",
        bestFor: "Outbound follow-up",
    },
    {
        id: "quill",
        name: "Quill",
        descriptor: "Even-tempered, never rushes the caller",
        accent: "UK — Received Pronunciation",
        tone: "calm",
        bestFor: "Support and retention",
    },
    {
        id: "juno",
        name: "Juno",
        descriptor: "Conversational, natural disfluencies",
        accent: "US — Northeast",
        tone: "warm",
        bestFor: "Appointment setting",
    },
    {
        id: "sable",
        name: "Sable",
        descriptor: "Composed and precise under pressure",
        accent: "US — Southern neutral",
        tone: "calm",
        bestFor: "High-value intake",
    },
];

export const TONE_STYLES: Record<VoiceTone, { label: string; className: string }> = {
    warm: {
        label: "Warm",
        className: "border-emerald-500/25 bg-emerald-500/10 text-emerald-300",
    },
    authoritative: {
        label: "Authoritative",
        className: "border-blue-500/25 bg-blue-500/10 text-blue-300",
    },
    energetic: {
        label: "Energetic",
        className: "border-amber-500/25 bg-amber-500/10 text-amber-300",
    },
    calm: {
        label: "Calm",
        className: "border-cyan-500/25 bg-cyan-500/10 text-cyan-300",
    },
};

/**
 * Deterministic per-voice waveform signature.
 *
 * Purely decorative: it gives each voice a stable visual identity so cards are
 * distinguishable at a glance. It is not derived from real audio.
 */
export function waveformSignature(voiceId: string, bars: number): number[] {
    let seed = 0;
    for (let i = 0; i < voiceId.length; i += 1) {
        seed = (seed * 31 + voiceId.charCodeAt(i)) % 100003;
    }

    return Array.from({ length: bars }, (_, i) => {
        seed = (seed * 1103515245 + 12345) % 2147483648;
        const noise = seed / 2147483648;
        // Envelope keeps the ends short so the shape reads as a spoken phrase.
        const envelope = Math.sin((Math.PI * (i + 1)) / (bars + 1));
        return 0.18 + noise * 0.55 * envelope + envelope * 0.27;
    });
}
