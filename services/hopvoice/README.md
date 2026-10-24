# Hopvoice — Hopwhistle Native Voice Engine

Hopvoice is Hopwhistle's first-party neural TTS: an internal GPU inference
service used by our self-hosted Dograh-derived AI Voice runtime. It is not a
hosted-vendor integration.

```
LLM -> Pipecat text aggregation -> HopvoiceTTSService (api/services/pipecat/hopvoice_tts.py)
    -> one persistent WebSocket per call (/v1/tts/ws)
    -> Qwen3-TTS Base on the GPU, frame-level streaming loop (hopvoice/qwen_stream.py)
    -> stateful resampler -> 20 ms PCM frames -> telephony transport -> caller
```

## What is implemented

- `native-fast` (Qwen3-TTS-12Hz-0.6B-Base) / `native-premium` (1.7B-Base)
  profiles, one profile per worker.
- Persistent `/v1/tts/ws` protocol (`start`, `text`, binary PCM, `cancel_turn`,
  `cancel_all`, `end`) and OpenAI-compatible `/v1/audio/speech`.
- **Frame-level streaming loop** (`hopvoice/qwen_stream.py`) over the pinned
  `rekuenkdr/Qwen3-TTS-streaming` model internals:
  - cancellation is checked after every codec frame (~80 ms of speech) instead
    of every emitted chunk (~1 s), so barge-in frees the GPU within one talker
    step;
  - chunk overlaps are time-aligned. The library loop blends the held-back tail
    of chunk N with a *different* time span of chunk N+1; on the Alex reference
    it deleted 277 ms from a 7.9 s sentence (SNR vs full decode −2.9 dB). The
    Hopvoice loop is sample-exact in length (SNR 19 dB; the residual is the
    shorter first-phase decode window, see Tuning);
  - per-turn timings: queue wait, prefill, first codec frame, first PCM.
  - `HOPVOICE_STREAM_LOOP=library` switches back to the library loop.
- **Stateful resampling** for 8/16/48 kHz sessions. Resampling each streamed
  chunk separately put a filter transient (up to 0.11 abs error) at every chunk
  boundary of the 8 kHz telephone stream; output now equals whole-utterance
  `resample_poly`.
- Runaway guard: codec frames are capped by text length.
- Per-session serialization, stale-speech suppression, GPU semaphore held until
  the producer thread really exits (no overlapping generations).
- **Delivery Planner** (`hopvoice/delivery.py`): per turn picks speech act,
  style reference, sampling temperature and a leading beat from the text and
  the previous turn. It never returns or modifies text.
- Metrics (`/metrics`): requests, failures, cancels, first-audio, prefill,
  cancel tail (cancel -> GPU slot released), RTF inputs, CUDA memory.

## Voice: Alex — Final Expense Center (`alex_fec_v1`)

`voices/alex_fec_v1/` holds a **fully synthetic** voice: Qwen3-TTS 1.7B
VoiceDesign generated it from a text description (`tools/design_voice.py`), so
no human recording is cloned. The Base workers condition on that clip's
speaker embedding (`x_vector_only_mode`), which gives a stable reusable
identity and bounded turn-start silence (measured 370 ms P50 / 540 ms max vs
~700 ms / 2.2 s with full-clip ICL, which continues from the reference's
pauses). `alex_fec_v1_icl` is the same speaker via ICL on the clip's last
sentence; it also copies the reference's casual delivery, so A/B the two by
ear. Six candidates, their
objective analysis and the provenance record are kept alongside it. The
selection was made on measurements (male register, calm pitch movement,
moderate pausing, 0 WER at 24 kHz and 8 kHz mu-law); confirm it by ear and swap
`ref_audio`/`ref_text` in `voices/voices.json` if another candidate wins.

Paths in `voices.json` are relative to the file, so the same registry works
mounted at `/voices` and from a checkout. Optional per-voice fields:
`temperature`, `top_p`, and `styles` (`{"warm": {"ref_audio": ..., "ref_text": ...}}`)
used by the Delivery Planner.

No public figure, commercial actor, customer, or employee voice may be added
without explicit synthetic-voice rights.

## Deploy on a GPU host

```bash
cd services/hopvoice
HOPVOICE_API_KEY=... HOPVOICE_BIND=<private-ip> \
  docker compose -f docker-compose.gpu.yaml up -d --build
curl http://<private-ip>:8787/healthz      # "loaded": true, "streaming_available": true
curl http://<private-ip>:8787/metrics      # hopvoice_cuda_memory_* present => model on GPU
```

`/healthz` alone is not a deployment check; run the benchmark below.

On the AI Voice runtime that should use it (one controlled test agent first):
`HOPVOICE_ENABLED=true`, TTS provider `hopvoice`, model `native-fast`, voice
`alex_fec_v1`, endpoint `http://<private-ip>:8787`, API key as above.

If the GPU host must run with `HF_HUB_OFFLINE=1`, set `HOPVOICE_MODEL_ID` to the
local snapshot directory: transformers 4.57.3 still calls the Hub for a repo id.

## Benchmark and listening pack

```bash
# Inside the GPU container (or any host with websockets + soundfile):
python3 -m bench.run_bench --endpoint ws://127.0.0.1:8787 --api-key $HOPVOICE_API_KEY \
    --voice alex_fec_v1 --profile native-fast --out /tmp/bench --repeats 3 --first-is-cold
```

`bench/final_expense_pack.json` holds the real agent phrases (opening, short
acknowledgments, qualification, objections, transfer, numbers, insurance terms,
questions, corrections, empathy, upbeat, hesitation). The harness writes
`results.json` / `utterances.csv` (TTFA, server TTFA breakdown, RTF, playout
underrun, P50/P95, cold vs warm, GPU snapshot), `wav/*_24k.wav` and
`wav/*_8k_mulaw.wav` (what the caller hears), a sentence-turn dialogue, and four
barge-in scenarios (stale audio after cancel, GPU cancel tail, next-turn TTFA).
`--analyze` (needs the `bench` extra) adds ASR word error rate, pitch, pace,
leading silence and speaker similarity per file.

## Tuning

```bash
python3 -m bench.sweep_stream --model Qwen/Qwen3-TTS-12Hz-0.6B-Base \
    --voices /voices/voices.json --voice alex_fec_v1 --out /tmp/sweep
```

Sweeps first-chunk emission/decode window, steady emission and overlap with the
model loaded once; reports model-side TTFA, prefill, RTF and discontinuities and
writes WAVs per setting. Pick the fastest setting that sounds clean, then set
`HOPVOICE_STREAM_*` on the worker. Starting values: first emission 5 frames,
first decode window 48, steady emission 12, steady window 80, overlap 512.
Fidelity data says short utterances (< ~3.8 s, decoded entirely in the first
phase) are least faithful with the 48-frame first window, so include 80 in the
sweep.

## No-GPU tests

```bash
pip install -e '.[test]' && python -m pytest -q
```

The stub backend covers the protocol. `tests/test_streaming_loop.py` drives the
frame-level loop with a fake causal model (needs torch; skipped otherwise) to
prove sample-exact stitching and cancellation within one codec frame.

## Still open

1. GPU numbers: TTFA / RTF / VRAM / cancel tail on the target GPU (tools above).
2. Real phone call on one controlled test agent; blind preference vs the
   current production voice.
3. Style references for the Delivery Planner (`styles` in voices.json) once the
   base Alex identity is approved by ear.
