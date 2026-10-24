"""Hopvoice's frame-level Qwen3-TTS streaming loop.

The pinned ``rekuenkdr/Qwen3-TTS-streaming`` runtime exposes
``stream_generate_voice_clone``. Hopvoice drives the same model internals here
instead, for three reasons measured against that loop:

1. Cancellation. The library loop can only be stopped when it yields, i.e. every
   ``emit_every_frames`` codec frames (12 frames = ~1 s of speech at 12 Hz).
   After a caller barge-in the GPU kept producing stale speech for up to that
   long, and the next turn waited for it. Here ``cancel_event`` is checked after
   every codec frame (one talker step), so the producer thread exits within one
   step and the GPU slot is freed for the next turn.
2. Chunk stitching. The library crossfades the held-back tail of chunk N
   (time T-ov..T) with the head of chunk N+1 (time T..T+ov). Those are different
   moments of speech: every boundary drops ``overlap_samples`` and blends two
   unrelated waveforms. The 12 Hz codec decoder is causal, so re-decoding a
   window reproduces earlier samples; this loop overlaps the *same* time span.
3. Timing. It records prefill, first codec frame and first PCM timestamps so
   TTFA can be broken down instead of measured only at the network edge.
4. Sampling parity. The library loop dropped two rules of the official
   generate(): min_new_tokens=2 and repetition_penalty=1.05. Without them
   short turns frequently produced no audio at all. With them this loop draws
   the same frames as the official generator for the same seed.
5. Leading silence. ICL cloning continues from the reference clip, so a turn
   often opens with a pause (measured 0.6-1.1 s, and occasionally a short
   acknowledgment came out entirely silent). Streaming that silence plays it to
   the caller in real time; the gate drops it and reports no_speech instead of
   streaming silence until the frame cap.

Generation itself (prefill, talker step, sampling, decode) is unchanged.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class StreamParams:
    emit_every_frames: int = 12
    decode_window_frames: int = 80
    overlap_samples: int = 512
    max_frames: int = 10000
    use_optimized_decode: bool = True
    first_chunk_emit_every: int = 5
    first_chunk_decode_window: int = 48
    first_chunk_frames: int = 48
    # Match the official Qwen3-TTS generate(): repetition_penalty 1.05 and
    # min_new_tokens=2 (codec EOS suppressed for the first two frames). The
    # library streaming loop used 1.0 and no minimum; on short lines that made
    # Alex say nothing ("Perfect."/"Sure." -> 0 frames in 6/6 seeds) or run on
    # into silence ("Got it." -> 6.4 s).
    repetition_penalty: float = 1.05
    min_new_frames: int = 2
    repetition_penalty_window: int = 100
    fade_in_samples: int = 96
    # Leading-silence gate: audio is not emitted until a 10 ms frame exceeds
    # speech_gate_dbfs; pre-roll keeps soft onsets. If no speech appears within
    # max_leading_silence_frames, generation stops with timings["no_speech"].
    trim_leading_silence: bool = True
    speech_gate_dbfs: float = -42.0
    preroll_ms: float = 60.0
    max_leading_silence_frames: int = 30


def supported(wrapper) -> bool:
    """True when the installed runtime has the internals this loop drives."""
    try:
        from qwen_tts.core.models import modeling_qwen3_tts as m
    except ImportError:
        return False
    model = getattr(wrapper, "model", None)
    return all(
        (
            hasattr(m, "_sample_next_token"),
            hasattr(m, "_add_ref_code_context"),
            callable(getattr(model, "_build_talker_inputs", None)),
            callable(getattr(wrapper, "_prompt_items_to_voice_clone_prompt", None)),
            callable(getattr(wrapper, "_tokenize_texts", None)),
            callable(getattr(wrapper, "_build_assistant_text", None)),
            callable(getattr(wrapper, "_build_ref_text", None)),
            callable(getattr(wrapper, "_merge_generate_kwargs", None)),
        )
    )


def _raised_cosine(n: int, rising: bool) -> np.ndarray:
    t = np.arange(n, dtype=np.float32) / max(n - 1, 1)
    ramp = 0.5 * (1.0 - np.cos(np.pi * t))
    return ramp if rising else ramp[::-1].copy()


def stream_voice_clone(
    wrapper,
    *,
    text: str,
    language: str,
    voice_clone_prompt,
    params: StreamParams,
    generation: dict,
    cancel_event: threading.Event | None = None,
    timings: dict | None = None,
) -> Iterator[tuple[np.ndarray, int]]:
    import torch
    from qwen_tts.core.models.modeling_qwen3_tts import (
        _add_ref_code_context,
        _sample_next_token,
    )

    timings = timings if timings is not None else {}
    timings["generation_start"] = time.perf_counter()

    def cancelled() -> bool:
        return bool(cancel_event and cancel_event.is_set())

    model = wrapper.model
    prompt_items = (
        voice_clone_prompt
        if isinstance(voice_clone_prompt, list)
        else [voice_clone_prompt]
    )
    prompt_dict = wrapper._prompt_items_to_voice_clone_prompt(prompt_items)
    input_ids = wrapper._tokenize_texts([wrapper._build_assistant_text(text)])
    ref_text = getattr(prompt_items[0], "ref_text", None)
    ref_ids = [
        wrapper._tokenize_texts([wrapper._build_ref_text(ref_text)])[0]
        if ref_text
        else None
    ]
    merged = wrapper._merge_generate_kwargs(**generation)
    do_sample = bool(merged.get("do_sample", True))
    top_k = int(merged.get("top_k", 50))
    top_p = float(merged.get("top_p", 1.0))
    temperature = float(merged.get("temperature", 0.9))
    sub = {
        "subtalker_dosample": bool(merged.get("subtalker_dosample", True)),
        "subtalker_top_k": int(merged.get("subtalker_top_k", 50)),
        "subtalker_top_p": float(merged.get("subtalker_top_p", 1.0)),
        "subtalker_temperature": float(merged.get("subtalker_temperature", 0.9)),
    }

    with torch.inference_mode():
        embeds, mask, trailing, pad_embed = model._build_talker_inputs(
            input_ids=input_ids,
            instruct_ids=None,
            ref_ids=ref_ids,
            voice_clone_prompt=prompt_dict,
            languages=[language or "Auto"],
            speakers=None,
            non_streaming_mode=False,
        )
        config = model.config
        eos_ids = {
            config.talker_config.codec_eos_token_id,
            2150,
            2157,
            151670,
            config.tts_eos_token_id,
            config.im_end_token_id,
            151643,
        }
        vocab_size = config.talker_config.vocab_size
        suppress = [i for i in range(vocab_size - 1024, vocab_size) if i not in eos_ids]

        torch.compiler.cudagraph_mark_step_begin()
        out = model.talker.forward(
            inputs_embeds=embeds,
            attention_mask=mask,
            use_cache=True,
            output_hidden_states=True,
            return_dict=True,
            trailing_text_hidden=trailing,
            tts_pad_embed=pad_embed,
            generation_step=None,
            past_hidden=None,
            past_key_values=None,
            **sub,
        )
        past_key_values = out.past_key_values
        past_hidden = out.past_hidden
        generation_step = out.generation_step
        codec_eos = config.talker_config.codec_eos_token_id

        def block_early_eos(logits, generated: int):
            # HF MinNewTokensLengthLogitsProcessor on the primary codec EOS.
            if generated < params.min_new_frames:
                logits = logits.clone()
                logits[..., codec_eos] = float("-inf")
            return logits

        logits = block_early_eos(out.logits[:, -1, :], 0)
        token = (
            _sample_next_token(logits, temperature, top_k, top_p, suppress)
            if do_sample
            else torch.argmax(logits, dim=-1)
        )
        timings["prefill_done"] = time.perf_counter()
        if cancelled():
            return

        ref_code = None
        ref_frames = 0
        ref_list = prompt_dict.get("ref_code")
        icl_list = prompt_dict.get("icl_mode")
        if (
            ref_list is not None
            and icl_list is not None
            and ref_list[0] is not None
            and icl_list[0]
        ):
            ref_code = ref_list[0].to(model.talker.device)
            ref_frames = ref_code.shape[0]

        rp = params.repetition_penalty
        if rp != 1.0:
            rp_window = params.repetition_penalty_window or params.max_frames
            rp_history = torch.full(
                (1, rp_window), vocab_size, device=token.device, dtype=torch.long
            )
            rp_history[0, 0] = token[0]
            rp_step = 1

        tokenizer = model.speech_tokenizer
        samples_per_frame = int(tokenizer.get_decode_upsample_rate())
        overlap = max(0, params.overlap_samples)
        codes: list = []
        emitted_frames = 0
        since_emit = 0
        held_tail: np.ndarray | None = None
        sample_rate = None

        def decode(window_codes, window_frames: int, optimized: bool):
            window, prefix = _add_ref_code_context(
                window_codes, ref_code, ref_frames, window_frames
            )
            window = window.to(model.talker.device)
            if optimized and hasattr(tokenizer, "decode_streaming"):
                wavs, sr = tokenizer.decode_streaming(
                    window, use_optimized=True, pad_to_size=params.decode_window_frames
                )
            else:
                wavs, sr = tokenizer.decode([{"audio_codes": window}])
            return np.asarray(wavs[0], dtype=np.float32), int(sr)

        speech_started = not params.trim_leading_silence
        timings["leading_silence_ms"] = 0.0

        def gate(chunk: np.ndarray, sr: int) -> np.ndarray | None:
            """Drop audio before the first speech frame (keeping pre-roll)."""
            nonlocal speech_started
            if speech_started or len(chunk) == 0:
                return chunk
            hop = max(1, sr // 100)
            frames = len(chunk) // hop
            first = None
            if frames:
                blocks = chunk[: frames * hop].reshape(frames, hop).astype(np.float64)
                level = 10.0 * np.log10(np.mean(blocks**2, axis=1) + 1e-12)
                above = np.nonzero(level > params.speech_gate_dbfs)[0]
                if len(above):
                    # Refine to the first loud sample inside that 10 ms block.
                    block = int(above[0]) * hop
                    amplitude = 10.0 ** (params.speech_gate_dbfs / 20.0)
                    loud = np.abs(chunk[block : block + hop]) > amplitude
                    first = block + (int(np.argmax(loud)) if loud.any() else 0)
            if first is None:
                timings["leading_silence_ms"] += len(chunk) / sr * 1000.0
                return None
            start = max(0, first - int(sr * params.preroll_ms / 1000.0))
            out = chunk[start:].copy()
            if start and params.fade_in_samples > 0:
                n = min(params.fade_in_samples, len(out))
                out[:n] *= _raised_cosine(n, rising=True)
            timings["leading_silence_ms"] += start / sr * 1000.0
            speech_started = True
            return out

        def stitch(wav: np.ndarray, new_samples: int, final: bool) -> np.ndarray:
            """Return audio for the newest ``new_samples``, overlapping the
            held-back tail of the previous chunk over the same time span."""
            nonlocal held_tail
            new_samples = min(new_samples, len(wav))
            chunk = wav[len(wav) - new_samples :].copy()
            if held_tail is not None and len(held_tail):
                span = len(held_tail)
                context = wav[len(wav) - new_samples - span : len(wav) - new_samples]
                if len(context) == span:
                    fade = _raised_cosine(span, rising=True)
                    blended = held_tail * (1.0 - fade) + context * fade
                else:  # not enough left context in this window; keep old tail
                    blended = held_tail
                chunk = np.concatenate([blended, chunk])
            elif params.fade_in_samples > 0:
                n = min(params.fade_in_samples, len(chunk))
                chunk[:n] *= _raised_cosine(n, rising=True)
            if final:
                held_tail = None
                n = min(overlap, len(chunk))
                if n:
                    chunk[-n:] *= _raised_cosine(n, rising=False)
                return chunk
            if overlap and len(chunk) > overlap:
                held_tail = chunk[-overlap:].copy()
                return chunk[:-overlap]
            held_tail = None
            return chunk

        for _ in range(params.max_frames):
            if cancelled():
                return
            torch.compiler.cudagraph_mark_step_begin()
            step = model.talker.forward(
                input_ids=token.unsqueeze(1),
                use_cache=True,
                return_dict=True,
                output_hidden_states=False,
                past_key_values=past_key_values,
                past_hidden=past_hidden,
                generation_step=generation_step,
                trailing_text_hidden=trailing,
                tts_pad_embed=pad_embed,
                **sub,
            )
            past_key_values = step.past_key_values
            past_hidden = step.past_hidden
            generation_step = step.generation_step
            codec_ids = step.hidden_states[1]
            if codec_ids[0, 0].item() in eos_ids:
                break
            codes.append(codec_ids[0].detach())
            if "first_frame" not in timings:
                timings["first_frame"] = time.perf_counter()

            logits = step.logits[:, -1, :]
            if rp != 1.0:
                presence = torch.zeros(
                    1, vocab_size + 1, device=logits.device, dtype=torch.bool
                )
                presence.scatter_(1, rp_history, True)
                penalized = torch.where(logits > 0, logits / rp, logits * rp)
                logits = torch.where(presence[:, :vocab_size], penalized, logits)
            logits = block_early_eos(logits, len(codes))
            token = (
                _sample_next_token(logits, temperature, top_k, top_p, suppress)
                if do_sample
                else torch.argmax(logits, dim=-1)
            )
            if rp != 1.0:
                rp_history[0, rp_step % rp_window] = token[0]
                rp_step += 1

            since_emit += 1
            first_phase = (
                params.first_chunk_emit_every > 0
                and len(codes) < params.first_chunk_frames
            )
            emit_every = (
                params.first_chunk_emit_every
                if first_phase
                else params.emit_every_frames
            )
            window_frames = (
                params.first_chunk_decode_window
                if first_phase
                else params.decode_window_frames
            )
            if since_emit < emit_every:
                continue
            since_emit = 0

            new_frames = len(codes) - emitted_frames
            start = max(0, len(codes) - window_frames)
            wav, sample_rate = decode(
                torch.stack(codes[start:], dim=0),
                window_frames,
                optimized=params.use_optimized_decode and not first_phase,
            )
            emitted_frames = len(codes)
            chunk = gate(
                stitch(wav, new_frames * samples_per_frame, final=False), sample_rate
            )
            if cancelled():
                return
            if chunk is None:
                if (
                    params.max_leading_silence_frames
                    and len(codes) >= params.max_leading_silence_frames
                ):
                    timings["no_speech"] = True
                    timings["frames"] = len(codes)
                    return
                continue
            if "first_pcm" not in timings:
                timings["first_pcm"] = time.perf_counter()
            timings["frames"] = len(codes)
            yield chunk, sample_rate

        if cancelled():
            return
        timings["frames"] = len(codes)
        remaining = len(codes) - emitted_frames
        if remaining > 0:
            context = min(emitted_frames, params.decode_window_frames - remaining)
            context = max(context, 0)
            wav, sample_rate = decode(
                torch.stack(codes[emitted_frames - context :], dim=0),
                params.decode_window_frames,
                optimized=False,
            )
            chunk = stitch(wav, remaining * samples_per_frame, final=True)
        elif held_tail is not None:
            chunk = held_tail
            n = min(overlap, len(chunk))
            chunk[-n:] *= _raised_cosine(n, rising=False)
        else:
            return
        chunk = gate(chunk, sample_rate or 24000)
        if chunk is None:
            timings["no_speech"] = True
            return
        if "first_pcm" not in timings:
            timings["first_pcm"] = time.perf_counter()
        yield chunk, sample_rate or 24000
