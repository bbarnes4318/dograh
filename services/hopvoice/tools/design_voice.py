"""Design a brand-new synthetic Hopvoice voice and save clone references.

Provenance: the voice is generated from a text description by
Qwen3-TTS-12Hz-1.7B-VoiceDesign (Apache-2.0). No human speaker's recording is
used, so there is no cloned person. The chosen candidate WAV + its exact text
become the ``ref_audio``/``ref_text`` of a Base-model voice in voices.json, which
is what gives a stable, reusable voice identity on the streaming worker.

Every candidate is written with a JSON manifest (model, instruct, text, seed,
sampling settings) so the selected reference can be regenerated and audited.

Usage:
    python -m tools.design_voice --model /models/Qwen3-TTS-12Hz-1.7B-VoiceDesign \
        --out voices/alex_fec_v1/candidates --seeds 11 23 --device cuda:0
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

ALEX_INSTRUCTS = {
    "warm_rep": (
        "A male American speaker in his late thirties with a warm, calm, slightly "
        "low voice. He sounds like a friendly, patient insurance representative "
        "chatting on the phone with an older customer: relaxed, confident, "
        "approachable and a little casual, with natural conversational pacing. "
        "General American accent. Not an announcer, not a radio voice, not "
        "salesy or over-enthusiastic."
    ),
    "easygoing": (
        "Mid-thirties American man, easygoing and trustworthy. Medium-low pitch, "
        "soft but clear, speaking at a relaxed everyday pace like he is talking "
        "to a neighbor. Friendly and reassuring, never pushy, never theatrical."
    ),
    "steady_pro": (
        "A calm, steady American male voice around forty years old. Confident "
        "and kind, conversational rather than scripted, warm mid-low tone, "
        "unhurried, with natural small pauses. Sounds like a real person on a "
        "phone call, not a commercial."
    ),
}

# Spoken by the designed voice and kept verbatim as ref_text. Conversational,
# varied in rhythm, and free of any insurance claims; ICL cloning carries this
# delivery style into every utterance, so it should sound like a phone call.
ALEX_REFERENCE_TEXT = (
    "Hey, how's it going? Yeah, so I'm just following up on that information "
    "you'd asked about. It's real simple, it only takes a minute. And honestly, "
    "if anything doesn't make sense, just stop me and I'll walk you through it."
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--model", required=True, help="VoiceDesign checkpoint path or HF id"
    )
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--seeds", nargs="+", type=int, default=[11, 23])
    parser.add_argument("--instructs", nargs="+", default=list(ALEX_INSTRUCTS))
    parser.add_argument("--text", default=ALEX_REFERENCE_TEXT)
    parser.add_argument("--language", default="English")
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--dtype", default="bfloat16")
    parser.add_argument("--temperature", type=float, default=0.8)
    args = parser.parse_args()

    import soundfile as sf
    import torch
    from qwen_tts import Qwen3TTSModel

    model = Qwen3TTSModel.from_pretrained(
        args.model, device_map=args.device, dtype=getattr(torch, args.dtype)
    )
    args.out.mkdir(parents=True, exist_ok=True)
    for name in args.instructs:
        instruct = ALEX_INSTRUCTS.get(name, name)
        for seed in args.seeds:
            torch.manual_seed(seed)
            started = time.perf_counter()
            wavs, sample_rate = model.generate_voice_design(
                text=args.text,
                instruct=instruct,
                language=args.language,
                temperature=args.temperature,
            )
            elapsed = time.perf_counter() - started
            stem = f"{name}_s{seed}"
            sf.write(args.out / f"{stem}.wav", wavs[0], sample_rate)
            manifest = {
                "candidate": stem,
                "model": str(args.model),
                "instruct": instruct,
                "ref_text": args.text,
                "language": args.language,
                "seed": seed,
                "temperature": args.temperature,
                "sample_rate": sample_rate,
                "duration_s": round(len(wavs[0]) / sample_rate, 3),
                "generation_s": round(elapsed, 2),
                "provenance": "synthetic: Qwen3-TTS VoiceDesign from text description; no human reference audio",
            }
            (args.out / f"{stem}.json").write_text(
                json.dumps(manifest, indent=2) + "\n"
            )
            print(json.dumps(manifest), flush=True)


if __name__ == "__main__":
    main()
