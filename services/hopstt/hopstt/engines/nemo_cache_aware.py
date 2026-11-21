"""NeMo cache-aware streaming adapter (Nemotron 3.5 ASR / Nemotron speech EN).

Implements the same algorithm as NeMo's
``examples/asr/asr_cache_aware_streaming/speech_to_text_cache_aware_streaming_infer.py``
(``CacheAwareStreamingAudioBuffer`` + ``conformer_stream_step``) but for live
audio and for many sessions per forward pass:

* **Streaming features.** ``CacheAwareStreamingAudioBuffer`` computes the mel
  spectrogram of a whole file up front. Here each session keeps a short raw
  audio tail and computes only the new mel frames on every chunk, from an
  audio segment aligned to the 10 ms hop that starts two hops before the
  first new frame. Every emitted frame's STFT window lies entirely in real
  audio, so frames are identical to the offline spectrogram (the stream is
  conceptually prefixed with the same zero padding ``center=True`` uses).
* **Pre-encode cache.** As in the buffer, every step after the first prepends
  the last ``pre_encode_cache_size`` mel frames and drops
  ``drop_extra_pre_encoded`` sub-sampled frames.
* **Encoder caches.** ``cache_last_channel`` / ``cache_last_time`` /
  ``cache_last_channel_len`` per session; sessions in one step are
  concatenated on the batch axis and split afterwards.
* **Decoder.** RNNT greedy-batch with per-session ``partial_hypotheses``.
* **Language prompt.** Nemotron 3.5 conditions on a language one-hot
  (``prompt_kernel``). NeMo's ``set_inference_prompt`` is model-global; we
  apply the same projection with a per-session prompt index so sessions with
  different languages share a batch.
* **Finalize.** Remaining audio plus enough silence to complete the chunk is
  pushed through, the final hypothesis is read, then the decoder hypothesis
  resets (encoder caches continue: the model has simply heard a pause).

Only float32 compute is supported by NeMo for cache-aware models.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .base import EngineError, Hypothesis, StreamingEngine, strip_language_tags

logger = logging.getLogger("hopstt.nemo")

HOP = 160  # 10 ms at 16 kHz
N_FFT_HALF = 256
VIRTUAL_PREFIX = 2 * HOP  # zeros prepended so frame k sits at segment index 2


@dataclass
class _ChunkCfg:
    chunk_ms: int
    att_context: list[int]
    first_chunk: int  # mel frames consumed by the first step
    shift: int  # mel frames per later step
    pre_cache_first: int
    pre_cache: int
    drop_extra: int


@dataclass
class NemoState:
    language: str
    chunk_ms: int
    prompt_index: int | None
    cache_channel: Any
    cache_time: Any
    cache_len: Any
    hyp: Any = None
    steps: int = 0
    # raw 16 kHz audio not yet framed, in "virtual" sample coordinates
    audio: np.ndarray = field(default_factory=lambda: np.zeros(VIRTUAL_PREFIX, np.float32))
    audio_start: int = 0  # virtual index of audio[0]
    next_frame: int = 0  # next mel frame index to compute
    mel_pending: Any = None  # (F, n) torch, computed but not consumed
    mel_cache: Any = None  # (F, pre_cache) last consumed frames
    detected_language: str | None = None
    fresh_audio: bool = False  # audio since last finalize
    freed: bool = False

    @property
    def pending_frames(self) -> int:
        return 0 if self.mel_pending is None else int(self.mel_pending.shape[-1])


class NemoCacheAwareEngine(StreamingEngine):
    name = "nemo-cache-aware"

    def __init__(self, spec, settings):
        super().__init__(spec, settings)
        self.model = None
        self.torch = None
        self.device = None
        self._chunk_cfgs: dict[int, _ChunkCfg] = {}
        self._active_chunk_ms: int | None = None
        self._prompts: dict[str, int] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def load(self) -> None:
        import torch
        import nemo.collections.asr as nemo_asr

        from ..weights import weight_file

        self.torch = torch
        self.device = torch.device(self.settings.device)
        path = weight_file(self.spec, self.settings)
        logger.info("loading %s from %s", self.spec.id, path)
        model = nemo_asr.models.ASRModel.restore_from(str(path), map_location=self.device)
        model.eval()
        model = model.to(device=self.device, dtype=torch.float32)
        torch.set_grad_enabled(False)
        featurizer = model.preprocessor.featurizer
        featurizer.dither = 0.0
        featurizer.pad_to = 0
        normalize = str(model.cfg.preprocessor.get("normalize", "NA"))
        if normalize not in ("NA", "None", "none"):
            # Per-utterance feature normalisation cannot be done online.
            raise EngineError(f"{self.spec.id}: preprocessor normalize={normalize} unsupported", transient=False)
        if hasattr(model.decoding, "set_strip_lang_tags"):
            # Keep the <xx-XX> tag so we can report the detected language.
            model.decoding.set_strip_lang_tags(False)
        self.model = model

        left = int(self.spec.options["left_context"])
        for chunk_ms, right in self.spec.options["right_context"].items():
            model.encoder.set_default_att_context_size([left, int(right)])
            cfg = model.encoder.streaming_cfg
            chunk = cfg.chunk_size if isinstance(cfg.chunk_size, list) else [cfg.chunk_size] * 2
            shift = cfg.shift_size if isinstance(cfg.shift_size, list) else [cfg.shift_size] * 2
            pre = (
                cfg.pre_encode_cache_size
                if isinstance(cfg.pre_encode_cache_size, list)
                else [cfg.pre_encode_cache_size] * 2
            )
            self._chunk_cfgs[int(chunk_ms)] = _ChunkCfg(
                chunk_ms=int(chunk_ms),
                att_context=[left, int(right)],
                first_chunk=int(chunk[0]),
                shift=int(shift[1]),
                pre_cache_first=int(pre[0]),
                pre_cache=int(pre[1]),
                drop_extra=int(cfg.drop_extra_pre_encoded),
            )
            if shift[1] * 10 != int(chunk_ms):
                raise EngineError(
                    f"{self.spec.id}: att_context {left},{right} gives {shift[1] * 10} ms chunks, expected {chunk_ms}",
                    transient=False,
                )
        self._active_chunk_ms = None

        prompt_dict = dict(model.cfg.get("model_defaults", {}).get("prompt_dictionary", {}) or {})
        self._has_prompt = bool(getattr(model, "concat", False)) and hasattr(model, "prompt_kernel")
        for lang, key in (self.spec.options.get("prompts") or {}).items():
            if key not in prompt_dict:
                raise EngineError(f"{self.spec.id}: prompt {key!r} not in checkpoint", transient=False)
            self._prompts[lang] = int(prompt_dict[key])
        self.loaded = True

    def warmup(self) -> None:
        rng = np.random.default_rng(0)
        for chunk_ms in self.spec.chunk_ms:
            state = self.new_state(self.spec.languages[0], chunk_ms)
            try:
                for _ in range(3):
                    chunk = (rng.standard_normal(16 * chunk_ms) * 0.01).astype(np.float32)
                    self.step([state], [chunk])
                self.finalize(state, np.zeros(0, np.float32))
            finally:
                self.free(state)
        self.warm = True

    def close(self) -> None:
        self.model = None

    # ------------------------------------------------------------------
    def _use_chunk(self, chunk_ms: int) -> _ChunkCfg:
        cfg = self._chunk_cfgs[chunk_ms]
        if self._active_chunk_ms != chunk_ms:
            self.model.encoder.set_default_att_context_size(cfg.att_context)
            self._active_chunk_ms = chunk_ms
        return cfg

    def new_state(self, language: str, chunk_ms: int) -> NemoState:
        if chunk_ms not in self._chunk_cfgs:
            raise EngineError(f"chunk_ms {chunk_ms} unsupported", transient=False)
        cache_channel, cache_time, cache_len = self.model.encoder.get_initial_cache_state(batch_size=1)
        prompt = self._prompts.get(language) if self._has_prompt else None
        return NemoState(
            language=language,
            chunk_ms=chunk_ms,
            prompt_index=prompt,
            cache_channel=cache_channel,
            cache_time=cache_time,
            cache_len=cache_len,
        )

    def free(self, state: NemoState) -> None:
        state.freed = True
        state.cache_channel = state.cache_time = state.cache_len = None
        state.hyp = None
        state.mel_pending = state.mel_cache = None
        state.audio = np.zeros(0, np.float32)

    def reset_utterance(self, state: NemoState) -> None:
        state.hyp = None
        state.fresh_audio = False

    # ------------------------------------------------------------------
    # streaming mel features
    # ------------------------------------------------------------------
    def _push_audio(self, state: NemoState, audio: np.ndarray) -> None:
        if len(audio):
            state.audio = np.concatenate([state.audio, audio.astype(np.float32, copy=False)])
        total = state.audio_start + len(state.audio)
        k = state.next_frame
        k_end = (total - N_FFT_HALF) // HOP - 1  # exclusive
        if k_end <= k:
            return
        seg_start = k * HOP - state.audio_start
        seg_len = (k_end - k + 1) * HOP + N_FFT_HALF
        segment = state.audio[seg_start : seg_start + seg_len]
        torch = self.torch
        signal = torch.from_numpy(np.ascontiguousarray(segment)).to(self.device).unsqueeze(0)
        length = torch.tensor([signal.shape[-1]], device=self.device)
        mel, _ = self.model.preprocessor(input_signal=signal, length=length)
        frames = mel[0, :, 2 : 2 + (k_end - k)]
        state.mel_pending = frames if state.mel_pending is None else torch.cat([state.mel_pending, frames], dim=-1)
        state.next_frame = k_end
        keep_from = k_end * HOP - state.audio_start
        state.audio = state.audio[keep_from:]
        state.audio_start = k_end * HOP

    def _restart_framing(self, state: NemoState) -> None:
        state.audio = np.zeros(VIRTUAL_PREFIX, np.float32)
        state.audio_start = 0
        state.next_frame = 0
        state.mel_pending = None

    def _finalize_padding(self, state: NemoState) -> int:
        """Silence (samples) needed so the chunk holding the last real mel
        frame is complete: exactly one more encoder step in the usual case,
        instead of padding a fixed amount that can cost two or three."""
        cfg = self._chunk_cfgs[state.chunk_ms]
        total = state.audio_start + len(state.audio)  # virtual end of real audio
        if total <= VIRTUAL_PREFIX and state.pending_frames == 0:
            return 0
        # last frame whose window touches real audio (frame k centred at (k+2)*HOP)
        k_last = (total - 1 + N_FFT_HALF) // HOP - 2
        unframed = max(0, k_last + 1 - state.next_frame)
        needed = state.pending_frames + unframed
        if needed <= 0:
            return 0
        if state.steps == 0 and needed <= cfg.first_chunk:
            target = cfg.first_chunk
        elif state.steps == 0:
            target = cfg.first_chunk + -(-(needed - cfg.first_chunk) // cfg.shift) * cfg.shift
        else:
            target = -(-needed // cfg.shift) * cfg.shift  # whole steps
        frames_to_add = target - state.pending_frames
        # frames become available while (k + 3) * HOP + N_FFT_HALF <= total
        want_end = state.next_frame + frames_to_add
        return max(0, (want_end + 1) * HOP + N_FFT_HALF - total)

    def _ready(self, state: NemoState) -> bool:
        cfg = self._chunk_cfgs[state.chunk_ms]
        need = cfg.first_chunk if state.steps == 0 else cfg.shift
        return state.pending_frames >= need

    # ------------------------------------------------------------------
    # batched encoder + decoder step
    # ------------------------------------------------------------------
    def _encode_group(self, states: list[NemoState], keep_all_outputs: bool = False) -> None:
        """One forward pass for sessions that are all ready and share
        chunk_ms and phase (first step vs later steps)."""
        torch = self.torch
        model = self.model
        cfg = self._use_chunk(states[0].chunk_ms)
        first = states[0].steps == 0
        size = cfg.first_chunk if first else cfg.shift
        pre = cfg.pre_cache_first if first else cfg.pre_cache
        feats = model.encoder._feat_in
        inputs = []
        for st in states:
            chunk = st.mel_pending[:, :size]
            if pre > 0:
                cache = st.mel_cache
                if cache is None:
                    cache = torch.zeros((feats, pre), device=self.device)
                elif cache.shape[-1] < pre:
                    cache = torch.cat(
                        [torch.zeros((feats, pre - cache.shape[-1]), device=self.device), cache], dim=-1
                    )
                chunk_in = torch.cat([cache, chunk], dim=-1)
            else:
                chunk_in = chunk
            inputs.append(chunk_in)
            consumed = st.mel_pending[:, :size]
            hist = consumed if st.mel_cache is None else torch.cat([st.mel_cache, consumed], dim=-1)
            st.mel_cache = hist[:, -cfg.pre_cache :] if cfg.pre_cache > 0 else None
            st.mel_pending = st.mel_pending[:, size:]
        signal = torch.stack(inputs, dim=0)
        lengths = torch.full((len(states),), signal.shape[-1], dtype=torch.long, device=self.device)
        cache_channel = torch.cat([s.cache_channel for s in states], dim=1)
        cache_time = torch.cat([s.cache_time for s in states], dim=1)
        cache_len = torch.cat([s.cache_len for s in states], dim=0)
        (encoded, encoded_len, cc_next, ct_next, cl_next) = model.encoder.cache_aware_stream_step(
            processed_signal=signal,
            processed_signal_length=lengths,
            cache_last_channel=cache_channel,
            cache_last_time=cache_time,
            cache_last_channel_len=cache_len,
            keep_all_outputs=keep_all_outputs,
            drop_extra_pre_encoded=0 if first else cfg.drop_extra,
        )
        encoded = self._apply_prompts(encoded, states)
        partial = [s.hyp for s in states]
        hyps = model.decoding.rnnt_decoder_predictions_tensor(
            encoder_output=encoded,
            encoded_lengths=encoded_len,
            return_hypotheses=True,
            partial_hypotheses=partial,
        )
        for i, st in enumerate(states):
            st.cache_channel = cc_next[:, i : i + 1].clone()
            st.cache_time = ct_next[:, i : i + 1].clone()
            st.cache_len = cl_next[i : i + 1].clone()
            st.hyp = hyps[i]
            st.steps += 1

    def _apply_prompts(self, encoded, states: list[NemoState]):
        if not self._has_prompt:
            return encoded
        torch = self.torch
        model = self.model
        default = self._prompts.get("auto", 0)
        idx = torch.tensor(
            [s.prompt_index if s.prompt_index is not None else default for s in states],
            dtype=torch.long,
            device=encoded.device,
        )
        x = encoded.transpose(1, 2)  # (B, T, D)
        b, t, _ = x.shape
        prompt = torch.zeros(b, t, model.num_prompts, dtype=x.dtype, device=x.device)
        prompt.scatter_(2, idx.view(b, 1, 1).expand(-1, t, -1), 1.0)
        x = model.prompt_kernel(torch.cat([x, prompt], dim=-1)).to(encoded.dtype)
        return x.transpose(1, 2)

    def _run_ready(self, states: list[NemoState], keep_all_outputs: bool = False) -> None:
        """Run encoder steps until no state in ``states`` has a full chunk."""
        while True:
            ready = [s for s in states if self._ready(s)]
            if not ready:
                return
            groups: dict[tuple, list[NemoState]] = {}
            for st in ready:
                groups.setdefault((st.chunk_ms, st.steps == 0), []).append(st)
            for group in groups.values():
                self._encode_group(group, keep_all_outputs=keep_all_outputs)

    def _hypothesis(self, state: NemoState) -> Hypothesis:
        raw = getattr(state.hyp, "text", "") if state.hyp is not None else ""
        if not isinstance(raw, str):
            raw = ""
        text, tag = strip_language_tags(raw)
        if tag:
            state.detected_language = tag
        language = state.language
        if language == "auto":
            language = state.detected_language or "auto"
        return Hypothesis(text=text, language=language, confidence=None)

    # ------------------------------------------------------------------
    def step(self, states, chunks):
        torch = self.torch
        try:
            with torch.inference_mode():
                for st, chunk in zip(states, chunks):
                    if st.freed:
                        raise EngineError("step on freed state", transient=False)
                    self._push_audio(st, chunk)
                    st.fresh_audio = True
                self._run_ready(list(states))
        except EngineError:
            raise
        except Exception as exc:  # pragma: no cover - real model only
            logger.exception("nemo step failed")
            raise EngineError(f"nemo step failed: {type(exc).__name__}") from exc
        return [self._hypothesis(st) for st in states]

    def finalize(self, state: NemoState, tail: np.ndarray) -> Hypothesis:
        torch = self.torch
        if not state.fresh_audio and not len(tail) and state.hyp is None:
            return Hypothesis(text="", language=state.language)
        try:
            with torch.inference_mode():
                self._push_audio(state, tail.astype(np.float32, copy=False))
                self._run_ready([state])
                pad = self._finalize_padding(state)
                if pad:
                    self._push_audio(state, np.zeros(pad, np.float32))
                    self._run_ready([state], keep_all_outputs=True)
                hyp = self._hypothesis(state)
        except Exception as exc:  # pragma: no cover - real model only
            logger.exception("nemo finalize failed")
            raise EngineError(f"nemo finalize failed: {type(exc).__name__}") from exc
        # New utterance: fresh decoder hypothesis and a fresh feature grid;
        # encoder caches carry on (they have just heard silence).
        # The language tag only follows terminal punctuation, so an utterance
        # without it reports the last language detected on this session.
        state.hyp = None
        state.fresh_audio = False
        self._restart_framing(state)
        return hyp
