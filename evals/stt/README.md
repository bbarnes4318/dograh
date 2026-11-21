# STT Evaluation Benchmark

Benchmark for comparing Speech-to-Text providers using **WebSocket streaming** with focus on:
- **Speaker diarization** - identifying who said what
- **Keyterm boosting** - improving recognition of specific terms (Deepgram)

## Providers

| Provider | Diarization | Keyterm Boost | Streaming |
|----------|-------------|---------------|-----------|
| Deepgram | Yes | Yes | WebSocket (v1/v2) |
| Speechmatics | Yes | Additional vocab | WebSocket RT |
| HopSTT (self-hosted) | No | Accepted, engine-dependent | WebSocket (`/v1/stt/stream`); set `HOPSTT_ENDPOINT`, optional `HOPSTT_API_KEY`, `HOPSTT_MODEL` |
| AssemblyAI | Optional (`--diarize`) | Yes (`keyterms_prompt`, max 100) | Streaming v3 (`wss://streaming.assemblyai.com/v3/ws`); set `ASSEMBLYAI_API_KEY`, optional `ASSEMBLYAI_MODEL` (default `assemblyai-stt-3-7-realtime-agent`) |

## Setup

```bash
# Install dependencies
pip install websockets

# Set API keys
export DEEPGRAM_API_KEY="your-key"
export SPEECHMATICS_API_KEY="your-key"
```

**Note:** Requires `ffmpeg` installed for audio conversion to PCM16.

## Usage

Run from the project root directory:

```bash
# Test both providers with diarization
python -m evals.stt.benchmark audio/multi_speaker.m4a --diarize

# Test only Deepgram
python -m evals.stt.benchmark audio/multi_speaker.m4a --diarize --providers deepgram

# Test with keyterm boosting (Deepgram)
python -m evals.stt.benchmark audio/multi_speaker.m4a --diarize --keyterms "Dograh" "Pipecat"

# Use different sample rate (default: 8000 Hz)
python -m evals.stt.benchmark audio/multi_speaker.m4a --diarize --sample-rate 16000

# Show word-level timings
python -m evals.stt.benchmark audio/multi_speaker.m4a --diarize --show-words

# Save results to JSON
python -m evals.stt.benchmark audio/multi_speaker.m4a --diarize --save
```

## Labeled comparison suite (phone agents)

`suite.py` runs every clip of a manifest through each provider and reports WER
(on clips with a reference), end-of-turn events vs expected (false / missed
endpoints), time to first partial, time from end of audio to the final, errors
and approximate cost per minute:

```bash
python -m evals.stt.suite suites/phone_agent.json --providers assemblyai deepgram-flux
python -m evals.stt.suite suites/phone_agent.json --providers assemblyai --mode min_latency
python -m evals.stt.suite suites/phone_agent.json --only short_answers ages states --save
```

`suites/phone_agent.json` covers short answers, ages, states, background talk,
long pauses, silence, voicemail, interruptions, false barge-in, Spanish and
English/Spanish switching. Most of its audio (`audio/phone/`) is not committed:
record synthetic or properly de-identified clips with exact reference text.
Missing clips are reported as SKIPPED, never as passes. Three existing clips
carry provisional labels taken from an earlier provider's output; verify them by
ear before trusting their WER. Small samples prove little: don't declare a
provider better without enough labeled clips.

Pipeline behaviour (barge-in, DNC and transfer handling, simultaneous calls,
tenant isolation, provider disconnects) is covered by the API unit tests
(`api/tests/test_assemblyai_*.py`), not by this file-level benchmark.

## CLI Options

| Option | Description |
|--------|-------------|
| `audio_file` | Path to audio file (relative to evals/stt/ or absolute) |
| `--providers` | Providers to test: `deepgram`, `speechmatics` (default: both) |
| `--diarize` | Enable speaker diarization |
| `--keyterms` | Keywords to boost (Deepgram) / additional vocab (Speechmatics) |
| `--language` | Language code (default: en) |
| `--sample-rate` | Audio sample rate for streaming (default: 8000) |
| `--show-words` | Show individual word timings |
| `--save` | Save results to JSON in `results/` |

## Directory Structure

```
evals/stt/
├── audio/              # Audio test files
│   └── multi_speaker.m4a
├── results/            # Saved benchmark results (JSON)
├── providers/          # STT provider implementations
│   ├── base.py         # Base classes
│   ├── deepgram_provider.py    # WebSocket streaming
│   └── speechmatics_provider.py # WebSocket streaming
├── audio_streamer.py   # PCM16 audio file streamer
├── benchmark.py        # Main runner script
└── README.md
```

## How It Works

1. **Audio Conversion**: The `AudioStreamer` converts any audio file to raw PCM16 using ffmpeg
2. **WebSocket Connection**: Providers connect to their respective WebSocket APIs
3. **Streaming**: Audio is sent in chunks (configurable sample rate, default 8kHz)
4. **Result Collection**: Transcripts and speaker info are collected from WebSocket responses
5. **Comparison**: Results are parsed into a common format for comparison

## Output Example

```
Audio file: /path/to/audio/multi_speaker.m4a
Providers: ['deepgram', 'speechmatics']
Diarization: True
Sample rate: 8000 Hz

============================================================
Provider: DEEPGRAM
============================================================

Duration: 45.32s
Speakers detected: 2 - ['0', '1']

Transcript:
Hello, welcome to the demo...

--- Speaker Segments ---
[0.0s] Speaker 0: Hello, welcome to the demo.
[2.5s] Speaker 1: Thanks for having me.
...

============================================================
COMPARISON SUMMARY
============================================================

Provider        Duration   Speakers   Words
---------------------------------------------
deepgram        45.32      2          312
speechmatics    45.32      2          308
```

## Adding New Providers

1. Create a new file in `providers/` (e.g., `whisper_provider.py`)
2. Implement the `STTProvider` abstract class with WebSocket streaming
3. Use `AudioStreamer` for PCM16 conversion
4. Add to `providers/__init__.py`
5. Add to `benchmark.py` provider choices

## API Documentation

- Deepgram Streaming: https://developers.deepgram.com/docs/live-streaming-audio
- Deepgram Diarization: https://developers.deepgram.com/docs/diarization
- Deepgram Keyterms: https://developers.deepgram.com/docs/keyterm
- Speechmatics RT API: https://docs.speechmatics.com/rt-api-ref
- Speechmatics Diarization: https://docs.speechmatics.com/features/diarization
