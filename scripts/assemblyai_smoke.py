"""Run one real AssemblyAI Streaming v3 session and print the result as JSON.

Proves the platform key, connectivity and model selection before a rollout.
Uses ASSEMBLYAI_API_KEY from the environment (never printed). Exit 0 = pass.

    python -m scripts.assemblyai_smoke                      # 1 s of silence, 8 kHz PCM16
    python -m scripts.assemblyai_smoke --wav sample.wav     # mono 16-bit WAV
    python -m scripts.assemblyai_smoke --wav sample.wav --encoding pcm_mulaw
    python -m scripts.assemblyai_smoke --language es --wav spanish.wav

Use synthetic or properly de-identified audio. Platform-key runs count against
the application budget and stream cap; --no-meter skips that (needs no Redis).
"""

import argparse
import asyncio
import json
import sys
import wave

from loguru import logger

from api.services.configuration.assemblyai import (
    ASSEMBLYAI_DEFAULT_MODEL,
    language_codes_for,
)
from api.services.pipecat.assemblyai_smoke import run_smoke_test


def read_wav(path: str) -> tuple[bytes, int]:
    with wave.open(path, "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise SystemExit(f"{path}: need mono 16-bit PCM WAV")
        return wav.readframes(wav.getnframes()), wav.getframerate()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--wav")
    parser.add_argument(
        "--encoding", choices=["pcm_s16le", "pcm_mulaw"], default="pcm_s16le"
    )
    parser.add_argument("--sample-rate", type=int, default=8000, help="when no --wav")
    parser.add_argument("--model", default=ASSEMBLYAI_DEFAULT_MODEL)
    parser.add_argument("--language", default="en", help="en, es or multi")
    parser.add_argument("--mode", default="balanced")
    parser.add_argument("--keyterm", action="append", default=None)
    parser.add_argument("--no-meter", action="store_true")
    args = parser.parse_args()
    logger.remove()

    audio, rate = (None, args.sample_rate)
    if args.wav:
        audio, rate = read_wav(args.wav)
        if args.encoding == "pcm_mulaw":
            import audioop

            audio = audioop.lin2ulaw(audio, 2)

    result = asyncio.run(
        run_smoke_test(
            audio=audio,
            sample_rate=rate,
            encoding=args.encoding,
            model=args.model,
            language_codes=language_codes_for(args.language),
            keyterms=args.keyterm,
            mode=args.mode,
            meter=not args.no_meter,
        )
    )
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
