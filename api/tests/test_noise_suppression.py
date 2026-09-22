"""Tests for inbound RNNoise noise suppression."""

import ctypes
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from pipecat.frames.frames import FilterEnableFrame

from api.schemas.workflow_configurations import (
    NoiseSuppressionConfiguration,
    WorkflowConfigurationDefaults,
)
from api.services.pipecat import noise_suppression
from api.services.pipecat.noise_suppression import (
    DEFAULT_RESAMPLER_QUALITY,
    RNNOISE_FRAME_SAMPLES,
    RNNoiseSuppressionFilter,
    build_audio_in_filter,
    load_library,
    reset_library_cache,
)
from api.services.pipecat.transport_params import audio_in_param_overrides

# The library is vendored in-tree, so these run everywhere the repo does — but
# the build is x86-64, so skip rather than fail on another architecture.
library_available = pytest.mark.skipif(
    load_library() is None, reason="librnnoise could not be loaded on this platform"
)


def _chunk(rng, samples: int, sigma: float = 3000.0) -> bytes:
    return rng.normal(0, sigma, samples).astype(np.int16).tobytes()


def _energy(audio: bytes) -> float:
    if not audio:
        return 0.0
    return float(np.sum(np.frombuffer(audio, dtype=np.int16).astype(np.float64) ** 2))


# A short clip of real recorded speech, vendored with pipecat.
_SPEECH_WAV = (
    Path(__file__).resolve().parents[2]
    / "pipecat/src/pipecat/services/aws/nova_sonic/ready.wav"
)


def _recorded_speech(rate: int) -> np.ndarray:
    """The sample clip, resampled to ``rate`` and repeated to a usable length."""
    if not _SPEECH_WAV.exists():
        pytest.skip(f"speech fixture not available at {_SPEECH_WAV}")

    import soxr

    with wave.open(str(_SPEECH_WAV)) as clip:
        source_rate = clip.getframerate()
        channels = clip.getnchannels()
        samples = np.frombuffer(clip.readframes(clip.getnframes()), dtype=np.int16)

    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1).astype(np.int16)
    resampled = soxr.resample(samples.astype(np.float32), source_rate, rate)
    return np.tile(resampled.astype(np.int16), 3)


class TestBuildAudioInFilter:
    def test_none_config_builds_nothing(self):
        assert build_audio_in_filter(None) is None

    def test_empty_config_builds_nothing(self):
        assert build_audio_in_filter({}) is None

    def test_disabled_builds_nothing(self):
        assert build_audio_in_filter({"enabled": False}) is None

    def test_enabled_builds_filter(self):
        built = build_audio_in_filter({"enabled": True})
        assert isinstance(built, RNNoiseSuppressionFilter)

    def test_quality_is_honoured(self):
        built = build_audio_in_filter({"enabled": True, "resampler_quality": "VHQ"})
        assert built._resampler_quality == "VHQ"

    def test_unknown_quality_falls_back_to_default(self):
        # A stored config predating the enum, or a hand-edited one, must not
        # take the call down.
        built = build_audio_in_filter(
            {"enabled": True, "resampler_quality": "nonsense"}
        )
        assert built._resampler_quality == DEFAULT_RESAMPLER_QUALITY


class TestTransportParamOverrides:
    def test_disabled_adds_no_params(self):
        assert audio_in_param_overrides(None) == {}
        assert audio_in_param_overrides({"enabled": False}) == {}

    def test_enabled_adds_audio_in_filter(self):
        overrides = audio_in_param_overrides({"enabled": True})
        assert set(overrides) == {"audio_in_filter"}
        assert isinstance(overrides["audio_in_filter"], RNNoiseSuppressionFilter)


class TestConfigurationSchema:
    def test_defaults_to_off(self):
        config = WorkflowConfigurationDefaults()
        assert config.noise_suppression.enabled is False
        assert config.noise_suppression.resampler_quality == "QQ"

    def test_explicit_null_falls_back_to_defaults(self):
        # Stored configs carry explicit JSON nulls for keys never configured.
        config = WorkflowConfigurationDefaults(noise_suppression=None)
        assert config.noise_suppression.enabled is False

    def test_rejects_unknown_quality(self):
        with pytest.raises(ValueError):
            NoiseSuppressionConfiguration(resampler_quality="TURBO")


class TestLibraryLoading:
    def test_wrong_frame_size_is_refused(self):
        """A build whose frame length disagrees would corrupt every frame."""
        reset_library_cache()
        try:
            fake = ctypes.CDLL(None)  # a real CDLL so attribute binding works
            with (
                patch.object(noise_suppression.ctypes, "CDLL", return_value=fake),
                patch.object(noise_suppression, "_bind"),
                patch.object(
                    type(fake), "rnnoise_get_frame_size", create=True
                ) as frame_size,
            ):
                frame_size.return_value = 123
                assert load_library() is None
        finally:
            reset_library_cache()

    def test_unloadable_library_is_reported_once(self):
        reset_library_cache()
        try:
            with patch.object(
                noise_suppression.ctypes, "CDLL", side_effect=OSError("nope")
            ) as cdll:
                assert load_library() is None
                calls_after_first = cdll.call_count
                # The negative result is cached: a broken install must not pay
                # the dlopen cost on every call.
                assert load_library() is None
                assert cdll.call_count == calls_after_first
        finally:
            reset_library_cache()


class TestFilterWithoutLibrary:
    async def test_audio_passes_through_untouched(self):
        """Losing the library must never cost audio, only suppression."""
        rng = np.random.default_rng(1)
        audio = _chunk(rng, 160)

        filt = RNNoiseSuppressionFilter()
        with patch.object(noise_suppression, "load_library", return_value=None):
            await filt.start(8000)

        assert await filt.filter(audio) == audio
        await filt.stop()


@library_available
class TestFilterBehaviour:
    async def test_suppresses_broadband_noise(self):
        """Pure noise should come back far quieter than it went in."""
        rng = np.random.default_rng(7)
        filt = RNNoiseSuppressionFilter()
        await filt.start(8000)

        went_in = came_out = 0.0
        for _ in range(50):
            audio = _chunk(rng, 160)
            went_in += _energy(audio)
            came_out += _energy(await filt.filter(audio))
        await filt.stop()

        assert went_in > 0
        # Measured around -38dB on this signal; assert a much weaker bound so
        # a library rebuild doesn't turn this into a flake.
        assert came_out / went_in < 0.1

    @pytest.mark.parametrize("rate", [8000, 16000])
    async def test_preserves_speech(self, rate):
        """Suppression must not swallow the caller along with the noise.

        Real recorded speech, not a synthetic signal: RNNoise classifies what
        it hears, and a tone or a harmonic stack is correctly judged non-speech
        and removed. Only actual speech exercises the property we care about.
        """
        speech = _recorded_speech(rate)

        filt = RNNoiseSuppressionFilter()
        await filt.start(rate)
        went_in = came_out = 0.0
        chunk = rate // 50
        for i in range(0, len(speech) - chunk + 1, chunk):
            audio = speech[i : i + chunk].tobytes()
            went_in += _energy(audio)
            came_out += _energy(await filt.filter(audio))
        await filt.stop()

        # Measured at roughly -0.8dB (≈0.83) against this clip; assert a much
        # weaker bound so a library rebuild doesn't turn this into a flake.
        assert came_out / went_in > 0.4

    async def test_buffers_until_a_whole_frame_is_available(self):
        """Sub-frame chunks return empty rather than short or padded audio."""
        rng = np.random.default_rng(3)
        filt = RNNoiseSuppressionFilter()
        await filt.start(48000)  # no resampling, so the arithmetic is exact

        # One sample short of a frame produces nothing...
        assert await filt.filter(_chunk(rng, RNNOISE_FRAME_SAMPLES - 1)) == b""
        # ...and the next chunk carries the held samples through.
        out = await filt.filter(_chunk(rng, RNNOISE_FRAME_SAMPLES + 1))
        assert len(out) == RNNOISE_FRAME_SAMPLES * 2 * 2
        await filt.stop()

    async def test_output_tracks_input_length(self):
        """Resampling round-trips must not drift the stream's duration."""
        rng = np.random.default_rng(5)
        filt = RNNoiseSuppressionFilter()
        await filt.start(8000)

        sent = produced = 0
        for _ in range(100):
            audio = _chunk(rng, 160)
            sent += len(audio)
            produced += len(await filt.filter(audio))
        await filt.stop()

        # Within one frame's worth of buffering lag.
        assert abs(sent - produced) <= RNNOISE_FRAME_SAMPLES * 2

    async def test_disable_frame_stops_filtering(self):
        rng = np.random.default_rng(11)
        filt = RNNoiseSuppressionFilter()
        await filt.start(48000)

        await filt.process_frame(FilterEnableFrame(enable=False))
        audio = _chunk(rng, RNNOISE_FRAME_SAMPLES)
        assert await filt.filter(audio) == audio

        await filt.process_frame(FilterEnableFrame(enable=True))
        assert await filt.filter(audio) != audio
        await filt.stop()

    async def test_stop_is_idempotent(self):
        """The transport stops the filter on end, cancel and cleanup."""
        filt = RNNoiseSuppressionFilter()
        await filt.start(8000)
        await filt.stop()
        await filt.stop()
        await filt.stop()

    async def test_filtering_after_stop_passes_through(self):
        """A late frame after teardown must not touch freed memory."""
        rng = np.random.default_rng(13)
        filt = RNNoiseSuppressionFilter()
        await filt.start(8000)
        await filt.stop()

        audio = _chunk(rng, 160)
        assert await filt.filter(audio) == audio

    async def test_empty_input_is_returned_unchanged(self):
        filt = RNNoiseSuppressionFilter()
        await filt.start(8000)
        assert await filt.filter(b"") == b""
        await filt.stop()

    @pytest.mark.parametrize("rate", [8000, 16000, 24000, 48000])
    async def test_runs_at_every_transport_rate(self, rate):
        """8k for most telephony, 16k for Vonage, 48k needs no resampling."""
        rng = np.random.default_rng(rate)
        filt = RNNoiseSuppressionFilter()
        await filt.start(rate)

        produced = 0
        for _ in range(20):
            produced += len(await filt.filter(_chunk(rng, rate // 50)))
        await filt.stop()

        assert produced > 0
