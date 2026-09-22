"""Inbound noise suppression, bound to the librnnoise that already ships in-tree.

``api/native/rnnoise/librnnoise.so`` has been committed since the pipecat bump,
and ``audio_mixer.librnnoise_path`` already pointed at it — but nothing ever
loaded the library and no transport ever set ``audio_in_filter``. Consumer
callers are in cars, kitchens and shops. That background noise is what drives
false barge-ins and garbage transcripts, and both cost conversions.

pipecat ships its own ``RNNoiseFilter``, but it binds ``pyrnnoise``, which is
neither installed nor declared as a dependency here. Adding a wheel next to a
library the repo already vendors buys nothing, so this binds the vendored
``.so`` directly — the C API is four functions.

RNNoise is fixed at 48 kHz and whole 480-sample frames, so narrowband telephony
audio has to be resampled up and back down. That costs CPU and a little
latency, and at 8 kHz the model is working well below the band it was trained
on, so the win is real but smaller than on wideband. It is therefore opt-in per
workflow rather than on by default.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os

import numpy as np
from loguru import logger

from api.constants import APP_ROOT_DIR
from pipecat.audio.filters.base_audio_filter import BaseAudioFilter
from pipecat.audio.resamplers.base_audio_resampler import SoxrQuality
from pipecat.frames.frames import FilterControlFrame, FilterEnableFrame

# Neither of these is configurable in RNNoise — the model is trained at one
# rate and consumes one frame length.
RNNOISE_SAMPLE_RATE = 48000
RNNOISE_FRAME_SAMPLES = 480

VENDORED_LIBRNNOISE_PATH = os.path.normpath(
    str(APP_ROOT_DIR / "native" / "rnnoise" / "librnnoise.so")
)

# The vendored build is x86-64. An operator on another architecture can point
# at their own build rather than losing the feature.
LIBRNNOISE_PATH_ENV_VAR = "DOGRAH_LIBRNNOISE_PATH"

VALID_RESAMPLER_QUALITIES = ("VHQ", "HQ", "MQ", "LQ", "QQ")
# Quick: this sits in the inbound audio path of a live call, so latency beats
# fidelity — and the signal is about to be denoised anyway.
DEFAULT_RESAMPLER_QUALITY: SoxrQuality = "QQ"

_library: ctypes.CDLL | None = None
_library_load_failed = False


def _candidate_library_paths() -> list[str]:
    """Where to look for librnnoise, best first."""
    candidates = []
    override = os.environ.get(LIBRNNOISE_PATH_ENV_VAR)
    if override:
        candidates.append(override)
    candidates.append(VENDORED_LIBRNNOISE_PATH)
    system = ctypes.util.find_library("rnnoise")
    if system:
        candidates.append(system)
    return candidates


def _bind(library: ctypes.CDLL) -> None:
    """Declare the four entry points we use.

    Without argtypes, ctypes defaults pointer arguments to ``c_int`` and
    truncates 64-bit pointers.
    """
    library.rnnoise_create.argtypes = [ctypes.c_void_p]
    library.rnnoise_create.restype = ctypes.c_void_p
    library.rnnoise_destroy.argtypes = [ctypes.c_void_p]
    library.rnnoise_destroy.restype = None
    library.rnnoise_get_frame_size.argtypes = []
    library.rnnoise_get_frame_size.restype = ctypes.c_int
    library.rnnoise_process_frame.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_float),
        ctypes.POINTER(ctypes.c_float),
    ]
    library.rnnoise_process_frame.restype = ctypes.c_float


def load_library() -> ctypes.CDLL | None:
    """Load librnnoise once per process, or ``None`` if it can't be loaded.

    A missing or unloadable library must never fail a call — the filter falls
    back to passing audio through untouched — so every failure here is logged
    and swallowed.
    """
    global _library, _library_load_failed

    if _library is not None:
        return _library
    if _library_load_failed:
        return None

    errors: list[str] = []
    for path in _candidate_library_paths():
        try:
            library = ctypes.CDLL(path)
            _bind(library)
        except (OSError, AttributeError) as e:
            errors.append(f"{path}: {e}")
            continue

        frame_size = library.rnnoise_get_frame_size()
        if frame_size != RNNOISE_FRAME_SAMPLES:
            # A build whose frame length disagrees would silently corrupt every
            # frame we hand it, so refuse it rather than denoise into garbage.
            errors.append(
                f"{path}: reports frame size {frame_size}, expected "
                f"{RNNOISE_FRAME_SAMPLES}"
            )
            continue

        logger.info(f"Loaded librnnoise from {path}")
        _library = library
        return _library

    _library_load_failed = True
    logger.error(
        "Noise suppression is enabled but librnnoise could not be loaded; "
        f"inbound audio will pass through unfiltered. Tried: {'; '.join(errors)}"
    )
    return None


def reset_library_cache() -> None:
    """Forget the cached handle. For tests only."""
    global _library, _library_load_failed
    _library = None
    _library_load_failed = False


class RNNoiseSuppressionFilter(BaseAudioFilter):
    """Suppress background noise on inbound caller audio.

    RNNoise only accepts whole 480-sample frames at 48 kHz, so audio is
    resampled to 48 kHz, buffered until at least one frame is available, and
    resampled back to the transport's rate on the way out. Most telephony
    transports run at 8 kHz; Vonage and WebRTC run at 16 kHz.

    One instance owns one denoiser state and therefore belongs to exactly one
    call. Build a new one per transport.

    Args:
        resampler_quality: SOXR preset used when the transport isn't already
            at 48 kHz. Higher quality costs more CPU per frame.
    """

    def __init__(self, *, resampler_quality: SoxrQuality = DEFAULT_RESAMPLER_QUALITY):
        self._resampler_quality: SoxrQuality = resampler_quality
        self._filtering = True
        self._sample_rate = 0
        self._library: ctypes.CDLL | None = None
        self._state: int | None = None
        self._resampler_in = None
        self._resampler_out = None
        # Holds 48 kHz PCM16 that hasn't filled a whole RNNoise frame yet.
        self._pending = bytearray()

    async def start(self, sample_rate: int):
        """Create the denoiser state and any resamplers this rate needs.

        Args:
            sample_rate: The transport's inbound sample rate in Hz.
        """
        self._sample_rate = sample_rate

        library = load_library()
        if library is None:
            return

        state = library.rnnoise_create(None)
        if not state:
            logger.error("rnnoise_create returned NULL; noise suppression disabled")
            return

        self._library = library
        self._state = state

        if sample_rate != RNNOISE_SAMPLE_RATE:
            try:
                from pipecat.audio.resamplers.soxr_stream_resampler import (
                    SOXRStreamAudioResampler,
                )
            except ImportError as e:
                logger.error(
                    f"Could not import SOXRStreamAudioResampler ({e}); "
                    "noise suppression disabled"
                )
                self._release_state()
                return

            # clear_after_secs=None: telephony chunks arrive with irregular
            # gaps, and dropping the resampler's history across one produces an
            # audible click at the seam.
            self._resampler_in = SOXRStreamAudioResampler(
                quality=self._resampler_quality, clear_after_secs=None
            )
            self._resampler_out = SOXRStreamAudioResampler(
                quality=self._resampler_quality, clear_after_secs=None
            )

        logger.info(
            f"RNNoise noise suppression active at {sample_rate} Hz "
            f"(resampler quality {self._resampler_quality})"
        )

    async def stop(self):
        """Release the denoiser state.

        The base transport calls this on ``EndFrame``, ``CancelFrame`` and
        again at cleanup, so it has to be idempotent.
        """
        self._release_state()
        self._resampler_in = None
        self._resampler_out = None
        self._pending.clear()

    def _release_state(self) -> None:
        if self._state is not None and self._library is not None:
            self._library.rnnoise_destroy(self._state)
        self._state = None
        self._library = None

    async def process_frame(self, frame: FilterControlFrame):
        """Honour runtime enable/disable without tearing down the state.

        Args:
            frame: The control frame to act on.
        """
        if isinstance(frame, FilterEnableFrame):
            self._filtering = frame.enable

    async def filter(self, audio: bytes) -> bytes:
        """Denoise one chunk of inbound PCM16 audio.

        Returns ``b""`` while buffering toward the first whole frame; the
        transport skips empty frames rather than forwarding them.

        Args:
            audio: Raw PCM16 audio at the transport's inbound sample rate.

        Returns:
            The denoised audio, or the input unchanged if the filter is off or
            unavailable.
        """
        if self._state is None or self._library is None or not self._filtering:
            return audio
        if not audio:
            return audio

        wideband = audio
        if self._resampler_in is not None:
            wideband = await self._resampler_in.resample(
                audio, self._sample_rate, RNNOISE_SAMPLE_RATE
            )

        self._pending.extend(wideband)

        frame_bytes = RNNOISE_FRAME_SAMPLES * 2
        whole_frames = len(self._pending) // frame_bytes
        if whole_frames == 0:
            return b""

        consumed = whole_frames * frame_bytes
        samples = np.frombuffer(bytes(self._pending[:consumed]), dtype=np.int16)
        del self._pending[:consumed]

        # RNNoise takes float samples on the int16 scale, not floats normalised
        # to [-1, 1]. Feeding it normalised input denoises near-silence and
        # returns near-silence.
        source = samples.astype(np.float32)
        denoised = np.empty_like(source)

        for index in range(whole_frames):
            start = index * RNNOISE_FRAME_SAMPLES
            end = start + RNNOISE_FRAME_SAMPLES
            # Slices of a contiguous 1-D array are themselves contiguous, so
            # these pointers address the frame in place.
            in_frame = source[start:end]
            out_frame = denoised[start:end]
            self._library.rnnoise_process_frame(
                self._state,
                out_frame.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                in_frame.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
            )

        output = np.clip(denoised, -32768.0, 32767.0).astype(np.int16).tobytes()

        if self._resampler_out is not None:
            output = await self._resampler_out.resample(
                output, RNNOISE_SAMPLE_RATE, self._sample_rate
            )
        return output


def build_audio_in_filter(
    noise_suppression_config: dict | None,
) -> BaseAudioFilter | None:
    """Build the inbound audio filter for a run, or ``None`` when disabled.

    Mirrors ``build_audio_out_mixer`` on the output side.

    Args:
        noise_suppression_config: The workflow's ``noise_suppression`` block.
    """
    if not noise_suppression_config or not noise_suppression_config.get(
        "enabled", False
    ):
        return None

    quality = noise_suppression_config.get(
        "resampler_quality", DEFAULT_RESAMPLER_QUALITY
    )
    if quality not in VALID_RESAMPLER_QUALITIES:
        logger.warning(
            f"Unknown resampler quality {quality!r}; using "
            f"{DEFAULT_RESAMPLER_QUALITY!r}"
        )
        quality = DEFAULT_RESAMPLER_QUALITY

    return RNNoiseSuppressionFilter(resampler_quality=quality)
