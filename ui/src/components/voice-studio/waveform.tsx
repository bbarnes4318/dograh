"use client";

import { useEffect, useRef, useState } from "react";

import { waveformSignature } from "./voice-catalog";

interface WaveformProps {
    voiceId: string;
    bars?: number;
    /** Animate the bars, as during playback. */
    active?: boolean;
    className?: string;
    /** Tailwind colour class applied to each bar. */
    barClassName?: string;
}

/**
 * Per-voice bar waveform.
 *
 * Static shape comes from the voice's deterministic signature so each voice
 * looks like itself; when `active` the bars breathe on a requestAnimationFrame
 * loop. Decorative — it does not analyse real audio.
 */
export function Waveform({
    voiceId,
    bars = 28,
    active = false,
    className = "",
    barClassName = "bg-slate-600",
}: WaveformProps) {
    const base = waveformSignature(voiceId, bars);
    const [phase, setPhase] = useState(0);
    const frame = useRef<number | undefined>(undefined);

    useEffect(() => {
        if (!active) {
            setPhase(0);
            return;
        }

        let start: number | null = null;
        const step = (t: number) => {
            if (start === null) start = t;
            setPhase((t - start) / 1000);
            frame.current = requestAnimationFrame(step);
        };
        frame.current = requestAnimationFrame(step);

        return () => {
            if (frame.current) cancelAnimationFrame(frame.current);
        };
    }, [active]);

    return (
        <div className={`flex items-center gap-[2px] ${className}`} aria-hidden="true">
            {base.map((amplitude, i) => {
                const modulated = active
                    ? amplitude * (0.55 + 0.45 * Math.abs(Math.sin(phase * 3.2 + i * 0.45)))
                    : amplitude;

                // Rounded to a fixed precision: the raw float differs in its last
                // digit between the server and client render, tripping hydration.
                const height = Math.max(2, modulated * 100).toFixed(2);

                return (
                    <span
                        key={i}
                        className={`w-[2px] shrink-0 rounded-full transition-[height] duration-75 ${barClassName}`}
                        style={{ height: `${height}%` }}
                    />
                );
            })}
        </div>
    );
}
