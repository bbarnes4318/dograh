"use client";

import { Bot, Gauge, MessageSquareText, Sparkles, Wand2 } from "lucide-react";
import { useState } from "react";

import type { StudioVoice } from "./voice-catalog";
import { Waveform } from "./waveform";

export interface AgentConfig {
    name: string;
    greeting: string;
    instructions: string;
    /** 0–100 slider positions. */
    pace: number;
    warmth: number;
    interruptible: boolean;
}

interface AgentDesignerProps {
    voice: StudioVoice;
    config: AgentConfig;
    onChange: (patch: Partial<AgentConfig>) => void;
}

type DesignerTab = "persona" | "script" | "behavior";

const TABS: { id: DesignerTab; label: string; icon: typeof Bot }[] = [
    { id: "persona", label: "Persona", icon: Bot },
    { id: "script", label: "Script", icon: MessageSquareText },
    { id: "behavior", label: "Behavior", icon: Gauge },
];

const inputClass =
    "w-full rounded-lg border border-slate-800 bg-slate-900/60 px-3 py-2 text-xs text-slate-200 placeholder:text-slate-600 focus:border-emerald-500/50 focus:outline-none focus:ring-1 focus:ring-emerald-500/30";

function Field({
    label,
    hint,
    children,
}: {
    label: string;
    hint?: string;
    children: React.ReactNode;
}) {
    return (
        <label className="block">
            <span className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">
                {label}
            </span>
            {hint && <span className="mt-0.5 block text-[11px] text-slate-600">{hint}</span>}
            <div className="mt-2">{children}</div>
        </label>
    );
}

function Slider({
    label,
    value,
    onChange,
    minLabel,
    maxLabel,
}: {
    label: string;
    value: number;
    onChange: (v: number) => void;
    minLabel: string;
    maxLabel: string;
}) {
    return (
        <label className="block">
            <div className="flex items-baseline justify-between">
                <span className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">
                    {label}
                </span>
                <span className="font-mono text-[11px] text-emerald-400">{value}</span>
            </div>
            <input
                type="range"
                min={0}
                max={100}
                value={value}
                onChange={(e) => onChange(Number(e.target.value))}
                className="mt-2 w-full accent-emerald-500"
            />
            <div className="mt-1 flex justify-between text-[10px] text-slate-600">
                <span>{minLabel}</span>
                <span>{maxLabel}</span>
            </div>
        </label>
    );
}

export function AgentDesigner({ voice, config, onChange }: AgentDesignerProps) {
    const [tab, setTab] = useState<DesignerTab>("persona");

    return (
        <section className="flex h-full flex-col rounded-xl border border-slate-800/80 bg-slate-950/40">
            <header className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-800/80 px-4 py-3">
                <div className="flex items-center gap-2">
                    <Sparkles className="h-4 w-4 text-cyan-400" />
                    <h2 className="text-sm font-semibold text-slate-200">Agent designer</h2>
                </div>

                <div className="flex items-center gap-1 rounded-lg border border-slate-800 bg-slate-900/60 p-0.5">
                    {TABS.map(({ id, label, icon: Icon }) => (
                        <button
                            key={id}
                            type="button"
                            onClick={() => setTab(id)}
                            className={`inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-[11px] font-semibold transition-colors ${
                                tab === id ? "bg-slate-800 text-white" : "text-slate-500 hover:text-slate-300"
                            }`}
                        >
                            <Icon className="h-3 w-3" />
                            {label}
                        </button>
                    ))}
                </div>
            </header>

            {/* Selected-voice banner */}
            <div className="flex items-center gap-4 border-b border-slate-800/80 bg-slate-900/20 px-4 py-3">
                <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-emerald-500/25 bg-emerald-500/10">
                    <Bot className="h-5 w-5 text-emerald-400" />
                </div>
                <div className="min-w-0 flex-1">
                    <p className="text-xs font-semibold text-white">
                        {config.name || "Untitled agent"}{" "}
                        <span className="font-normal text-slate-500">speaking as {voice.name}</span>
                    </p>
                    <p className="truncate text-[11px] text-slate-600">
                        {voice.accent} · {voice.descriptor}
                    </p>
                </div>
                <Waveform
                    voiceId={voice.id}
                    bars={22}
                    className="hidden h-6 w-28 sm:flex"
                    barClassName="bg-emerald-400/40"
                />
            </div>

            <div className="min-h-0 flex-1 overflow-y-auto p-4">
                {tab === "persona" && (
                    <div className="space-y-4">
                        <Field label="Agent name" hint="Shown on transcripts and call logs.">
                            <input
                                value={config.name}
                                onChange={(e) => onChange({ name: e.target.value })}
                                className={inputClass}
                                placeholder="Inbound Lead Qualifier"
                            />
                        </Field>

                        <Field label="Opening line" hint="The first thing the caller hears.">
                            <textarea
                                value={config.greeting}
                                onChange={(e) => onChange({ greeting: e.target.value })}
                                rows={3}
                                className={`${inputClass} resize-none leading-relaxed`}
                                placeholder="Hi, this is Nova with Hopwhistle — do you have a quick minute?"
                            />
                        </Field>

                        <div className="rounded-lg border border-slate-800 bg-slate-900/40 p-3">
                            <div className="flex items-center gap-2 text-[11px] font-semibold text-slate-400">
                                <Wand2 className="h-3 w-3 text-cyan-400" />
                                Opening line preview
                            </div>
                            <p className="mt-2 text-sm italic leading-relaxed text-slate-300">
                                “{config.greeting || "Your opening line will appear here."}”
                            </p>
                        </div>
                    </div>
                )}

                {tab === "script" && (
                    <Field
                        label="Instructions"
                        hint="Plain-language rules the agent follows for the whole call."
                    >
                        <textarea
                            value={config.instructions}
                            onChange={(e) => onChange({ instructions: e.target.value })}
                            rows={16}
                            className={`${inputClass} resize-none font-mono text-[12px] leading-relaxed`}
                            placeholder="You are qualifying inbound callers for…"
                        />
                    </Field>
                )}

                {tab === "behavior" && (
                    <div className="space-y-6">
                        <Slider
                            label="Speaking pace"
                            value={config.pace}
                            onChange={(pace) => onChange({ pace })}
                            minLabel="Deliberate"
                            maxLabel="Brisk"
                        />
                        <Slider
                            label="Warmth"
                            value={config.warmth}
                            onChange={(warmth) => onChange({ warmth })}
                            minLabel="Neutral"
                            maxLabel="Personable"
                        />

                        <label className="flex cursor-pointer items-start justify-between gap-4 rounded-lg border border-slate-800 bg-slate-900/40 p-3">
                            <span>
                                <span className="block text-xs font-semibold text-slate-200">
                                    Allow interruptions
                                </span>
                                <span className="mt-1 block text-[11px] leading-relaxed text-slate-500">
                                    The agent stops speaking the moment the caller talks over it.
                                </span>
                            </span>
                            <input
                                type="checkbox"
                                checked={config.interruptible}
                                onChange={(e) => onChange({ interruptible: e.target.checked })}
                                className="mt-0.5 h-4 w-4 shrink-0 accent-emerald-500"
                            />
                        </label>
                    </div>
                )}
            </div>
        </section>
    );
}
