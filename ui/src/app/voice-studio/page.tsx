"use client";

import { Blocks, Radio, Rows3, ShieldCheck, Waves, Zap } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { AgentDesigner, type AgentConfig } from "@/components/voice-studio/agent-designer";
import { RehearsalPanel } from "@/components/voice-studio/rehearsal-panel";
import { VOICE_CATALOG, type StudioVoice } from "@/components/voice-studio/voice-catalog";
import { VoiceLibrary } from "@/components/voice-studio/voice-library";

/**
 * The agent hub. It lists the org's agents (each card opens the
 * /workflow/[workflowId] builder, which owns the live-call UI at
 * /workflow/[workflowId]/run) and carries the create button. It is also where
 * the app itself lands users after sign-in — see app/after-sign-in/page.tsx.
 */
const BUILDER_ROUTE = "/workflow";

/** Matches any catalog voice name, so the opening line can follow the selection. */
const VOICE_NAME_PATTERN = new RegExp(`\\b(?:${VOICE_CATALOG.map((v) => v.name).join("|")})\\b`);

const DEFAULT_CONFIG: AgentConfig = {
    name: "Inbound Lead Qualifier",
    greeting: "Hi, this is Nova with Hopwhistle — do you have a quick minute?",
    instructions: [
        "You are qualifying inbound callers.",
        "",
        "Confirm the caller is the decision maker, capture their state and the",
        "coverage they are asking about, then hand off to a licensed agent.",
        "",
        "Keep answers to one or two sentences. Never quote a price.",
        "If the caller asks to be removed, confirm and end the call politely.",
    ].join("\n"),
    pace: 55,
    warmth: 70,
    interruptible: true,
};

const ACCENTS = {
    emerald: "text-emerald-400",
    cyan: "text-cyan-400",
    blue: "text-blue-400",
    indigo: "text-indigo-400",
} as const;

function Stat({
    icon: Icon,
    label,
    value,
    accent,
}: {
    icon: typeof Waves;
    label: string;
    value: string;
    accent: keyof typeof ACCENTS;
}) {
    return (
        <div className="rounded-xl border border-slate-800/80 bg-slate-950/50 px-3 py-2.5 backdrop-blur-sm">
            <dt className="flex items-center justify-between text-[10px] text-slate-500">
                {label}
                <Icon className={`h-3 w-3 ${ACCENTS[accent]}`} />
            </dt>
            <dd className="mt-1 font-mono text-lg font-bold leading-none">{value}</dd>
        </div>
    );
}

export default function VoiceStudioPage() {
    const router = useRouter();
    const [voice, setVoice] = useState<StudioVoice>(VOICE_CATALOG[0]);
    const [config, setConfig] = useState<AgentConfig>(DEFAULT_CONFIG);

    const patchConfig = (patch: Partial<AgentConfig>) =>
        setConfig((prev) => ({ ...prev, ...patch }));

    const openBuilder = () => router.push(BUILDER_ROUTE);

    return (
        <div className="flex min-h-screen flex-col bg-[#070913] text-white">
            {/* Ambient background. Clipped so the oversized glows can't widen the page. */}
            <div className="pointer-events-none fixed inset-0 overflow-hidden">
                <div className="absolute inset-0 bg-[linear-gradient(to_right,#0f172a50_1px,transparent_1px),linear-gradient(to_bottom,#0f172a50_1px,transparent_1px)] bg-[size:32px_32px]" />
                <div className="absolute left-1/2 top-0 h-[320px] w-[720px] -translate-x-1/2 rounded-full bg-emerald-500/10 blur-[130px]" />
                <div className="absolute left-1/4 top-1/3 h-[260px] w-[420px] rounded-full bg-cyan-500/[0.07] blur-[110px]" />
            </div>

            {/* Top bar */}
            <header className="sticky top-0 z-20 border-b border-slate-900 bg-[#070913]/85 backdrop-blur-md">
                <div className="mx-auto flex h-14 max-w-[1600px] items-center justify-between gap-4 px-4 md:px-6">
                    <div className="flex items-center gap-3">
                        <div className="flex h-8 w-8 items-center justify-center rounded-lg border border-emerald-500/25 bg-emerald-500/10">
                            <Waves className="h-4 w-4 text-emerald-400" />
                        </div>
                        <div className="leading-tight">
                            <p className="text-sm font-bold tracking-tight">Voice Studio</p>
                            <p className="hidden text-[10px] text-slate-500 sm:block">by Hopwhistle</p>
                        </div>
                    </div>

                    <div className="flex items-center gap-3">
                        <span className="hidden items-center gap-1.5 rounded-full border border-emerald-500/20 bg-emerald-500/10 px-2.5 py-1 text-[10px] font-semibold text-emerald-400 md:inline-flex">
                            <Radio className="h-2.5 w-2.5 animate-pulse" />
                            SIP/TLS
                        </span>
                        <button
                            type="button"
                            onClick={openBuilder}
                            className="inline-flex h-9 items-center gap-1.5 rounded-lg bg-emerald-500 px-4 text-xs font-semibold text-slate-950 shadow-lg shadow-emerald-500/10 transition-all hover:bg-emerald-400 active:scale-[0.98]"
                        >
                            <Rows3 className="h-3 w-3" />
                            Launch agent
                        </button>
                    </div>
                </div>
            </header>

            <main className="relative mx-auto w-full max-w-[1600px] flex-1 px-4 py-6 md:px-6 md:py-8">
                {/* Header band */}
                <div className="flex flex-col gap-6 lg:flex-row lg:items-end lg:justify-between">
                    <div className="max-w-2xl">
                        <h1 className="text-3xl font-extrabold leading-[1.1] tracking-tight md:text-4xl">
                            Build a voice your callers{" "}
                            <span className="bg-gradient-to-r from-emerald-400 via-cyan-400 to-blue-500 bg-clip-text text-transparent">
                                actually stay on the line for.
                            </span>
                        </h1>
                        <p className="mt-3 text-sm leading-relaxed text-slate-400 md:text-base">
                            Pick a voice, shape the persona, hear the opening at your chosen pace — then
                            take it live in the agent builder. Everything on this page is yours to change.
                        </p>
                    </div>

                    <dl className="grid shrink-0 grid-cols-2 gap-3 sm:grid-cols-4 lg:w-auto">
                        <Stat
                            icon={Waves}
                            label="Voices"
                            value={String(VOICE_CATALOG.length)}
                            accent="emerald"
                        />
                        <Stat icon={Zap} label="Pace" value={String(config.pace)} accent="cyan" />
                        <Stat
                            icon={ShieldCheck}
                            label="Barge-in"
                            value={config.interruptible ? "On" : "Off"}
                            accent="blue"
                        />
                        <Stat icon={Blocks} label="Transport" value="SIP/TLS" accent="indigo" />
                    </dl>
                </div>

                {/* Workspace */}
                <div className="mt-8 grid gap-4 lg:h-[calc(100vh-19rem)] lg:min-h-[560px] lg:grid-cols-12">
                    <div className="min-w-0 lg:col-span-3 lg:h-full lg:min-h-0">
                        <VoiceLibrary
                            selectedId={voice.id}
                            onSelect={(v) => {
                                setVoice(v);
                                // Keep the opening line in sync with the chosen voice's name.
                                setConfig((prev) => ({
                                    ...prev,
                                    greeting: prev.greeting.replace(VOICE_NAME_PATTERN, v.name),
                                }));
                            }}
                        />
                    </div>

                    <div className="min-w-0 lg:col-span-6 lg:h-full lg:min-h-0">
                        <AgentDesigner voice={voice} config={config} onChange={patchConfig} />
                    </div>

                    <div className="min-w-0 lg:col-span-3 lg:h-full lg:min-h-0">
                        <RehearsalPanel voice={voice} config={config} onOpenBuilder={openBuilder} />
                    </div>
                </div>
            </main>
        </div>
    );
}
