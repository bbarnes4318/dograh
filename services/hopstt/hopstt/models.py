"""Catalogue of the models HopSTT knows how to serve.

A model id (the wire-level ``model`` field) maps to one engine adapter and one
pinned Hugging Face revision. Changing models never changes the protocol; it
only changes which entry ``HOPSTT_ALLOWED_MODELS`` enables.

Revisions are the ``main`` commit of each repo on 2026-11-21 (fetched from
``https://huggingface.co/api/models/<id>``). ``sha256`` values are the LFS
object hashes Hugging Face publishes for the weight file we load; the fetch
tool verifies them after download.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ModelSpec:
    id: str
    kind: str  # "streaming" | "batch"
    engine: str  # adapter key in hopstt.engines.ENGINE_FACTORIES
    hf_repo: str | None
    revision: str | None
    languages: tuple[str, ...]
    chunk_ms: tuple[int, ...] = ()
    default_chunk_ms: int | None = None
    # Files fetched from the repo (globs for snapshot_download). The first
    # entry of ``weights`` is the file the engine loads.
    files: tuple[str, ...] = ()
    sha256: dict[str, str] = field(default_factory=dict)
    license: str = ""
    word_timestamps: bool = False
    # Engine-specific knobs (e.g. attention context per chunk size).
    options: dict = field(default_factory=dict)


NEMOTRON_35 = ModelSpec(
    id="nemotron-3.5-streaming",
    kind="streaming",
    engine="nemo_cache_aware",
    hf_repo="nvidia/nemotron-3.5-asr-streaming-0.6b",
    revision="ea30d66debe3740a08b573244286791d423d6b3e",
    languages=("en", "es", "auto"),
    chunk_ms=(80, 160, 320, 560, 1120),
    default_chunk_ms=320,
    files=("nemotron-3.5-asr-streaming-0.6b.nemo",),
    sha256={
        "nemotron-3.5-asr-streaming-0.6b.nemo": "210214ed94039bf6bfbb9a047c7fa289628db75b103e2bf6381fa78285436a74"
    },
    license="OpenMDW-1.1",
    options={
        "left_context": 56,
        # chunk_ms -> right context in 80 ms encoder frames (chunk = 1 + right).
        "right_context": {80: 0, 160: 1, 320: 3, 560: 6, 1120: 13},
        # Prompt keys in the checkpoint's prompt_dictionary.
        "prompts": {"en": "en-US", "es": "es-US", "auto": "auto"},
    },
)

NEMOTRON_EN = ModelSpec(
    id="nemotron-en-streaming",
    kind="streaming",
    engine="nemo_cache_aware",
    hf_repo="nvidia/nemotron-speech-streaming-en-0.6b",
    revision="ebe59e5a817142986528bbbee5dba8db7b38ed50",
    languages=("en",),
    chunk_ms=(80, 160, 560, 1120),
    default_chunk_ms=560,
    files=("nemotron-speech-streaming-en-0.6b.nemo",),
    sha256={
        "nemotron-speech-streaming-en-0.6b.nemo": "283638054c44f6794e74fe9af9048d78a6d9d6c058c12131856c7859a62ac9cd"
    },
    license="NVIDIA Open Model License",
    options={
        "left_context": 70,
        "right_context": {80: 0, 160: 1, 560: 6, 1120: 13},
        "prompts": {},
    },
)

VOXTRAL_REALTIME = ModelSpec(
    id="voxtral-realtime",
    kind="streaming",
    engine="vllm_realtime",
    hf_repo="mistralai/Voxtral-Mini-4B-Realtime-2602",
    revision="2769294da9567371363522aac9bbcfdd19447add",
    languages=("en", "es", "auto"),
    # Granularity at which audio is forwarded to vLLM; the model's own
    # transcription delay is set on the vLLM server (tekken.json,
    # transcription_delay_ms, multiple of 80, 480 recommended).
    chunk_ms=(80, 160, 240, 320, 480),
    default_chunk_ms=80,
    files=(),  # weights live on the vLLM server
    license="Apache-2.0",
)

PARAKEET_TDT_V3 = ModelSpec(
    id="parakeet-tdt-0.6b-v3",
    kind="batch",
    engine="nemo_offline",
    hf_repo="nvidia/parakeet-tdt-0.6b-v3",
    revision="541d1f99c6b0c3cd0b11a95167540bb8edefd82b",
    languages=("en", "es", "auto"),
    files=("parakeet-tdt-0.6b-v3.nemo",),
    sha256={
        "parakeet-tdt-0.6b-v3.nemo": "3cbdc85877e668ca7b82d0d56770eb1fac76691f55d6b97545e8d61ca588d10d"
    },
    license="CC-BY-4.0",
    word_timestamps=True,
)

WHISPER_TURBO = ModelSpec(
    id="whisper-large-v3-turbo",
    kind="batch",
    engine="faster_whisper",
    hf_repo="dropbox-dash/faster-whisper-large-v3-turbo",
    revision="0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf",
    languages=("en", "es", "auto"),
    files=(
        "model.bin",
        "config.json",
        "preprocessor_config.json",
        "tokenizer.json",
        "vocabulary.json",
    ),
    sha256={"model.bin": "e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da"},
    license="MIT",
    word_timestamps=True,
)

# Small Whisper for CPU development and smoke tests only.
WHISPER_BASE = ModelSpec(
    id="whisper-base",
    kind="batch",
    engine="faster_whisper",
    hf_repo="Systran/faster-whisper-base",
    revision="ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66",
    languages=("en", "es", "auto"),
    files=("model.bin", "config.json", "tokenizer.json", "vocabulary.txt"),
    sha256={"model.bin": "d01c3014881c9c6f3133c182f3d2887eb6ca1c789a7538c5c007196857a0a6a9"},
    license="MIT",
    word_timestamps=True,
)

QWEN3_ASR = ModelSpec(
    id="qwen3-asr-1.7b",
    kind="batch",
    engine="qwen3_asr",
    hf_repo="Qwen/Qwen3-ASR-1.7B",
    revision="7278e1e70fe206f11671096ffdd38061171dd6e5",
    languages=("en", "es", "auto"),
    files=("*.json", "*.safetensors", "merges.txt", "vocab.json"),
    sha256={
        "model-00001-of-00002.safetensors": "a4cd1f1a04d90b757dc7f7dd26254e69a013b19e80efe590a83c6a3bde8608d6",
        "model-00002-of-00002.safetensors": "6e0b9d9e09e2e0238e7ef3cc8a484ab387e91b90f1900bedf88bc92d7929ccfc",
    },
    license="Apache-2.0",
    # Word timestamps need the separate Qwen3-ForcedAligner; not wired.
    word_timestamps=False,
)

STUB_STREAMING = ModelSpec(
    id="stub-streaming",
    kind="streaming",
    engine="stub",
    hf_repo=None,
    revision=None,
    languages=("en", "es", "auto"),
    chunk_ms=(80, 160, 320, 560, 1120),
    default_chunk_ms=320,
    license="n/a (test engine)",
)

STUB_BATCH = ModelSpec(
    id="stub-batch",
    kind="batch",
    engine="stub_batch",
    hf_repo=None,
    revision=None,
    languages=("en", "es", "auto"),
    license="n/a (test engine)",
    word_timestamps=True,
)

CATALOG: dict[str, ModelSpec] = {
    spec.id: spec
    for spec in (
        NEMOTRON_35,
        NEMOTRON_EN,
        VOXTRAL_REALTIME,
        PARAKEET_TDT_V3,
        WHISPER_TURBO,
        WHISPER_BASE,
        QWEN3_ASR,
        STUB_STREAMING,
        STUB_BATCH,
    )
}
