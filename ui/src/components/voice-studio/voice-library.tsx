"use client";

import { Check, Pause, Play, Search, Volume2 } from "lucide-react";
import { useMemo, useRef, useState } from "react";

import { TONE_STYLES, VOICE_CATALOG, type StudioVoice } from "./voice-catalog";
import { Waveform } from "./waveform";

interface VoiceLibraryProps {
    selectedId: string;
    onSelect: (voice: StudioVoice) => void;
}

export function VoiceLibrary({ selectedId, onSelect }: VoiceLibraryProps) {
    const [query, setQuery] = useState("");
    const [playingId, setPlayingId] = useState<string | null>(null);
    const audioRef = useRef<HTMLAudioElement | null>(null);

    const voices = useMemo(() => {
        const q = query.trim().toLowerCase();
        if (!q) return VOICE_CATALOG;
        return VOICE_CATALOG.filter((v) =>
            [v.name, v.descriptor, v.accent, v.bestFor].some((field) =>
                field.toLowerCase().includes(q),
            ),
        );
    }, [query]);

    const togglePreview = (voice: StudioVoice) => {
        if (!voice.previewUrl) return;

        if (playingId === voice.id) {
            audioRef.current?.pause();
            setPlayingId(null);
            return;
        }

        audioRef.current?.pause();
        const audio = new Audio(voice.previewUrl);
        audio.onended = () => setPlayingId(null);
        audioRef.current = audio;
        void audio.play().then(
            () => setPlayingId(voice.id),
            () => setPlayingId(null),
        );
    };

    return (
        <section className="flex h-full flex-col rounded-xl border border-slate-800/80 bg-slate-950/40">
            <header className="flex items-center justify-between border-b border-slate-800/80 px-4 py-3">
                <div className="flex items-center gap-2">
                    <Volume2 className="h-4 w-4 text-emerald-400" />
                    <h2 className="text-sm font-semibold text-slate-200">Voice library</h2>
                </div>
                <span className="font-mono text-[11px] text-slate-500">{voices.length} voices</span>
            </header>

            <div className="border-b border-slate-800/80 p-3">
                <div className="relative">
                    <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-slate-600" />
                    <input
                        value={query}
                        onChange={(e) => setQuery(e.target.value)}
                        placeholder="Search tone, accent, use case…"
                        className="h-9 w-full rounded-lg border border-slate-800 bg-slate-900/60 pl-9 pr-3 text-xs text-slate-200 placeholder:text-slate-600 focus:border-emerald-500/50 focus:outline-none focus:ring-1 focus:ring-emerald-500/30"
                    />
                </div>
            </div>

            <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-3">
                {voices.map((voice) => {
                    const selected = voice.id === selectedId;
                    const playing = playingId === voice.id;
                    const tone = TONE_STYLES[voice.tone];

                    return (
                        <button
                            key={voice.id}
                            type="button"
                            onClick={() => onSelect(voice)}
                            aria-pressed={selected}
                            className={`group w-full rounded-lg border p-3 text-left transition-all ${
                                selected
                                    ? "border-emerald-500/50 bg-emerald-500/[0.07] shadow-[0_0_0_1px_rgba(16,185,129,0.15)]"
                                    : "border-slate-800 bg-slate-900/40 hover:border-slate-700 hover:bg-slate-900/70"
                            }`}
                        >
                            <div className="flex items-start justify-between gap-2">
                                <div className="min-w-0">
                                    <div className="flex items-center gap-2">
                                        <span className="text-sm font-semibold text-white">{voice.name}</span>
                                        {selected && <Check className="h-3.5 w-3.5 shrink-0 text-emerald-400" />}
                                    </div>
                                    <p className="mt-0.5 truncate text-[11px] text-slate-500">{voice.descriptor}</p>
                                </div>

                                {voice.previewUrl && (
                                    <span
                                        role="button"
                                        tabIndex={0}
                                        aria-label={
                                            playing ? `Pause ${voice.name} preview` : `Play ${voice.name} preview`
                                        }
                                        onClick={(e) => {
                                            e.stopPropagation();
                                            togglePreview(voice);
                                        }}
                                        onKeyDown={(e) => {
                                            if (e.key === "Enter" || e.key === " ") {
                                                e.preventDefault();
                                                e.stopPropagation();
                                                togglePreview(voice);
                                            }
                                        }}
                                        className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-slate-700 bg-slate-900 text-slate-300 transition-colors hover:border-emerald-500/50 hover:text-emerald-400"
                                    >
                                        {playing ? (
                                            <Pause className="h-3 w-3" />
                                        ) : (
                                            <Play className="ml-[1px] h-3 w-3" />
                                        )}
                                    </span>
                                )}
                            </div>

                            <Waveform
                                voiceId={voice.id}
                                bars={34}
                                active={playing}
                                className="mt-3 h-7 w-full"
                                barClassName={
                                    selected ? "bg-emerald-400/70" : "bg-slate-700 group-hover:bg-slate-600"
                                }
                            />

                            <div className="mt-3 flex flex-wrap items-center gap-1.5">
                                <span
                                    className={`rounded border px-1.5 py-0.5 text-[10px] font-semibold ${tone.className}`}
                                >
                                    {tone.label}
                                </span>
                                <span className="rounded border border-slate-800 bg-slate-900 px-1.5 py-0.5 font-mono text-[10px] text-slate-500">
                                    {voice.accent}
                                </span>
                            </div>

                            <p className="mt-2 text-[10px] text-slate-600">Best for {voice.bestFor}</p>
                        </button>
                    );
                })}

                {voices.length === 0 && (
                    <p className="px-1 py-8 text-center text-xs text-slate-600">
                        No voices match “{query}”.
                    </p>
                )}
            </div>
        </section>
    );
}
