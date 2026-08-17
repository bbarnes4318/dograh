"use client";

import { ExternalLink, PhoneCall, RotateCcw, Square } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import type { AgentConfig } from "./agent-designer";
import type { StudioVoice } from "./voice-catalog";
import { Waveform } from "./waveform";

interface RehearsalPanelProps {
    voice: StudioVoice;
    config: AgentConfig;
    /** Sends the operator to the real agent builder, where live calls are placed. */
    onOpenBuilder: () => void;
}

interface Turn {
    speaker: "agent" | "caller";
    text: string;
}

/**
 * Script rehearsal.
 *
 * Walks the configured opening through a scripted caller exchange so an
 * operator can feel the pacing before committing. This is a local dry run of
 * the copy they typed — no call is placed and no audio is synthesised. Live
 * test calls happen in the agent builder.
 */
export function RehearsalPanel({ voice, config, onOpenBuilder }: RehearsalPanelProps) {
    const [running, setRunning] = useState(false);
    const [turns, setTurns] = useState<Turn[]>([]);
    const timers = useRef<ReturnType<typeof setTimeout>[]>([]);
    const scrollRef = useRef<HTMLDivElement>(null);

    const script: Turn[] = [
        { speaker: "agent", text: config.greeting || "Thanks for calling — how can I help?" },
        { speaker: "caller", text: "Sure, I have a minute. What is this about?" },
        {
            speaker: "agent",
            text: "I just need two quick details to point you to the right person.",
        },
        { speaker: "caller", text: "Go ahead." },
    ];

    const clearTimers = () => {
        timers.current.forEach(clearTimeout);
        timers.current = [];
    };

    useEffect(() => clearTimers, []);

    useEffect(() => {
        scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
    }, [turns]);

    const start = () => {
        clearTimers();
        setTurns([]);
        setRunning(true);

        // Pace tracks the configured speaking pace: brisk settings advance faster.
        const step = 1600 - config.pace * 8;

        script.forEach((turn, i) => {
            timers.current.push(
                setTimeout(
                    () => {
                        setTurns((prev) => [...prev, turn]);
                        if (i === script.length - 1) setRunning(false);
                    },
                    step * (i + 1),
                ),
            );
        });
    };

    const stop = () => {
        clearTimers();
        setRunning(false);
    };

    return (
        <section className="flex h-full flex-col rounded-xl border border-slate-800/80 bg-slate-950/40">
            <header className="flex items-center justify-between border-b border-slate-800/80 px-4 py-3">
                <div className="flex items-center gap-2">
                    <PhoneCall className="h-4 w-4 text-blue-400" />
                    <h2 className="text-sm font-semibold text-slate-200">Rehearsal</h2>
                </div>
                {running && (
                    <span className="inline-flex items-center gap-1.5 rounded border border-emerald-500/25 bg-emerald-500/10 px-2 py-0.5 text-[10px] font-semibold text-emerald-400">
                        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-400" />
                        Running
                    </span>
                )}
            </header>

            <div className="border-b border-slate-800/80 px-4 py-3">
                <div className="flex items-center gap-3 rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2.5">
                    <Waveform
                        voiceId={voice.id}
                        bars={18}
                        active={running}
                        className="h-6 w-20 shrink-0"
                        barClassName={running ? "bg-emerald-400" : "bg-slate-700"}
                    />
                    <div className="min-w-0 flex-1">
                        <p className="truncate text-[11px] font-semibold text-slate-300">{voice.name}</p>
                        <p className="truncate text-[10px] text-slate-600">
                            {config.name || "Untitled agent"}
                        </p>
                    </div>
                    <button
                        type="button"
                        onClick={running ? stop : start}
                        className={`inline-flex h-8 shrink-0 items-center gap-1.5 rounded-lg px-3 text-[11px] font-semibold transition-colors ${
                            running
                                ? "border border-slate-700 bg-slate-900 text-slate-300 hover:bg-slate-800"
                                : "bg-emerald-500 text-slate-950 hover:bg-emerald-400"
                        }`}
                    >
                        {running ? (
                            <>
                                <Square className="h-3 w-3" /> Stop
                            </>
                        ) : turns.length > 0 ? (
                            <>
                                <RotateCcw className="h-3 w-3" /> Replay
                            </>
                        ) : (
                            <>
                                <PhoneCall className="h-3 w-3" /> Rehearse
                            </>
                        )}
                    </button>
                </div>
                <p className="mt-2 text-[10px] leading-relaxed text-slate-600">
                    A local dry run of your script — no call is placed and no audio is generated.
                </p>
            </div>

            <div ref={scrollRef} className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
                {turns.length === 0 && !running && (
                    <p className="py-10 text-center text-[11px] leading-relaxed text-slate-600">
                        Press <span className="text-slate-400">Rehearse</span> to walk through
                        <br />
                        your opening at the configured pace.
                    </p>
                )}

                {turns.map((turn, i) => (
                    <div
                        key={i}
                        className={`flex ${turn.speaker === "agent" ? "justify-start" : "justify-end"}`}
                    >
                        <div
                            className={`max-w-[85%] rounded-lg border px-3 py-2 ${
                                turn.speaker === "agent"
                                    ? "border-emerald-500/20 bg-emerald-500/[0.07]"
                                    : "border-slate-800 bg-slate-900/60"
                            }`}
                        >
                            <span
                                className={`text-[9px] font-semibold uppercase tracking-wider ${
                                    turn.speaker === "agent" ? "text-emerald-400" : "text-slate-500"
                                }`}
                            >
                                {turn.speaker === "agent" ? voice.name : "Caller"}
                            </span>
                            <p className="mt-1 text-[11px] leading-relaxed text-slate-300">{turn.text}</p>
                        </div>
                    </div>
                ))}

                {running && (
                    <div className="flex justify-start">
                        <div className="flex gap-1 rounded-lg border border-slate-800 bg-slate-900/60 px-3 py-2.5">
                            {[0, 1, 2].map((i) => (
                                <span
                                    key={i}
                                    className="h-1.5 w-1.5 animate-bounce rounded-full bg-slate-600"
                                    style={{ animationDelay: `${i * 120}ms` }}
                                />
                            ))}
                        </div>
                    </div>
                )}
            </div>

            <footer className="border-t border-slate-800/80 p-3">
                <button
                    type="button"
                    onClick={onOpenBuilder}
                    className="inline-flex h-9 w-full items-center justify-center gap-2 rounded-lg border border-slate-700 bg-slate-900/60 text-[11px] font-semibold text-slate-300 transition-colors hover:border-emerald-500/40 hover:text-white"
                >
                    Place a live test call
                    <ExternalLink className="h-3 w-3" />
                </button>
            </footer>
        </section>
    );
}
