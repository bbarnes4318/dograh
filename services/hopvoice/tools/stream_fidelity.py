"""Compare streamed audio against a single full decode of the same codec frames.

With the same seed both streaming loops draw identical codec frames (they call
the sampler in the same order; decoding uses no RNG), so any difference from the full decode is introduced purely by chunking/stitching.
Reports length error, SNR vs the ideal waveform and the worst local jump.

    python -m tools.stream_fidelity --model /models/Qwen3-TTS-12Hz-0.6B-Base \
        --ref voices/alex_fec_v1/reference.wav --ref-text-file voices/alex_fec_v1/reference.txt
"""

from __future__ import annotations

import argparse
import json

import numpy as np


def snr_db(ideal: np.ndarray, test: np.ndarray) -> float:
    n = min(len(ideal), len(test))
    err = ideal[:n] - test[:n]
    return round(
        float(
            10 * np.log10((np.sum(ideal[:n] ** 2) + 1e-12) / (np.sum(err**2) + 1e-12))
        ),
        2,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--ref-text-file", required=True)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--max-frames", type=int, default=240)
    parser.add_argument("--out", help="directory to write ideal/hopvoice/library WAVs")
    parser.add_argument(
        "--text",
        default="What usually happens next is the licensed agent asks you a few simple health questions, and then walks you through a couple of options.",
    )
    args = parser.parse_args()

    import torch
    from qwen_tts import Qwen3TTSModel
    from qwen_tts.core.models.modeling_qwen3_tts import _add_ref_code_context

    from hopvoice import qwen_stream
    from tools.voice_metrics import boundary_discontinuity

    dtype = torch.float32 if args.device == "cpu" else torch.bfloat16
    wrapper = Qwen3TTSModel.from_pretrained(
        args.model, device_map=args.device, dtype=dtype
    )
    ref_text = open(args.ref_text_file).read().strip()
    prompt = wrapper.create_voice_clone_prompt(ref_audio=args.ref, ref_text=ref_text)
    sampling = {"temperature": 0.8, "top_p": 0.95}
    params = qwen_stream.StreamParams(
        use_optimized_decode=False, max_frames=args.max_frames
    )

    captured: list = []
    original_forward = wrapper.model.talker.forward

    def capture(*a, **kw):
        out = original_forward(*a, **kw)
        if kw.get("input_ids") is not None and out.hidden_states is not None:
            captured.append(out.hidden_states[1][0].detach().clone())
        return out

    wrapper.model.talker.forward = capture
    torch.manual_seed(args.seed)
    hop = np.concatenate(
        [
            c
            for c, _ in qwen_stream.stream_voice_clone(
                wrapper,
                text=args.text,
                language="English",
                voice_clone_prompt=prompt,
                params=params,
                generation=sampling,
            )
        ]
    )
    hop_codes = list(captured)
    captured.clear()
    torch.manual_seed(args.seed)
    lib = np.concatenate(
        [
            c
            for c, _ in wrapper.stream_generate_voice_clone(
                text=args.text,
                language="English",
                voice_clone_prompt=prompt,
                emit_every_frames=params.emit_every_frames,
                decode_window_frames=params.decode_window_frames,
                overlap_samples=params.overlap_samples,
                use_optimized_decode=False,
                first_chunk_emit_every=params.first_chunk_emit_every,
                first_chunk_decode_window=params.first_chunk_decode_window,
                first_chunk_frames=params.first_chunk_frames,
                max_frames=args.max_frames,
                **sampling,
            )
        ]
    )
    lib_codes = list(captured)
    wrapper.model.talker.forward = original_forward

    eos = wrapper.model.config.talker_config.codec_eos_token_id
    codes = [c for c in hop_codes if int(c[0]) < eos]
    same_codes = len(lib_codes) == len(hop_codes) and all(
        torch.equal(a, b) for a, b in zip(lib_codes, hop_codes)
    )
    frames = torch.stack(codes, dim=0)
    prompt_item = prompt[0] if isinstance(prompt, list) else prompt
    ref_code = prompt_item.ref_code
    window, prefix = _add_ref_code_context(
        frames, ref_code, ref_code.shape[0], frames.shape[0] + ref_code.shape[0]
    )
    wavs, sr = wrapper.model.speech_tokenizer.decode([{"audio_codes": window}])
    ideal = np.asarray(wavs[0], dtype=np.float32)
    ideal = ideal[prefix * wrapper.model.speech_tokenizer.get_decode_upsample_rate() :]

    report = {
        "frames": len(codes),
        "ideal_samples": len(ideal),
        "hopvoice": {
            "samples": len(hop),
            "length_error": len(hop) - len(ideal),
            "snr_db": snr_db(ideal, hop),
            "max_jump_ratio": boundary_discontinuity(hop),
        },
        "library": {
            "samples": len(lib),
            "length_error": len(lib) - len(ideal),
            "snr_db": snr_db(ideal, lib),
            "max_jump_ratio": boundary_discontinuity(lib),
        },
        "ideal_max_jump_ratio": boundary_discontinuity(ideal),
        "identical_codes": same_codes,
    }
    print(json.dumps(report, indent=2))
    if args.out:
        import os

        import soundfile as sf

        os.makedirs(args.out, exist_ok=True)
        for name, audio in (("ideal", ideal), ("hopvoice", hop), ("library", lib)):
            sf.write(os.path.join(args.out, f"fidelity_{name}.wav"), audio, sr)


if __name__ == "__main__":
    main()
