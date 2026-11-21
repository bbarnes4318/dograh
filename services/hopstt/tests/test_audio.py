import subprocess

import numpy as np
import pytest
from scipy.signal import resample_poly

from conftest import HAS_FFMPEG, pcm16, tone
from hopstt.audio import (
    ALAW_TO_PCM16,
    ULAW_TO_PCM16,
    Chunker,
    DurationExceeded,
    FrameDecoder,
    StreamingResampler,
    Timeline,
    decode_alaw,
    decode_file,
    decode_mulaw,
    decode_pcm16,
)


def test_g711_reference_points():
    # ITU G.711 reference values (Sun g711.c).
    assert ULAW_TO_PCM16[0xFF] == 0
    assert ULAW_TO_PCM16[0x7F] == 0
    assert ULAW_TO_PCM16[0x00] == -32124
    assert ULAW_TO_PCM16[0x80] == 32124
    assert ALAW_TO_PCM16[0xD5] == 8
    assert ALAW_TO_PCM16[0x55] == -8
    assert ALAW_TO_PCM16[0xAA] == 32256
    assert ALAW_TO_PCM16[0x2A] == -32256
    # monotone within each sign half
    pos = ULAW_TO_PCM16[0x80:0x100]
    assert np.all(np.diff(pos.astype(int)) < 0)


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
@pytest.mark.parametrize("fmt,decoder", [("mulaw", decode_mulaw), ("alaw", decode_alaw)])
def test_g711_matches_ffmpeg(tmp_path, fmt, decoder):
    codes = bytes(range(256)) * 4
    src = tmp_path / f"in.{fmt}"
    src.write_bytes(codes)
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-f", fmt, "-ar", "8000", "-ac", "1", "-i", str(src), "-f", "s16le", "-"],
        capture_output=True,
        check=True,
    ).stdout
    ref = np.frombuffer(out, "<i2")
    ours = np.round(decoder(codes) * 32768).astype(np.int16)
    assert np.array_equal(ref, ours)


def test_odd_byte_frames_are_carried():
    audio = tone(0.3)
    data = pcm16(audio)
    whole = decode_pcm16(data)
    dec = FrameDecoder("pcm_s16le")
    parts = []
    i = 0
    for size in [1, 3, 7, 2, 101, 5, 999, 4]:
        parts.append(dec.decode(data[i : i + size]))
        i += size
    parts.append(dec.decode(data[i:]))
    got = np.concatenate(parts)
    assert dec.pending_bytes == 0
    assert np.array_equal(got, whole)


@pytest.mark.parametrize("src,dst", [(8000, 16000), (16000, 16000)])
def test_streaming_resampler_equals_whole_signal(src, dst):
    rng = np.random.default_rng(1)
    x = rng.standard_normal(src * 3).astype(np.float32) * 0.2
    rs = StreamingResampler(src, dst)
    out, i = [], 0
    while i < len(x):
        n = int(rng.integers(1, 700))
        out.append(rs.process(x[i : i + n]))
        i += n
    out.append(rs.flush())
    got = np.concatenate(out)
    ref = resample_poly(x, dst // np.gcd(src, dst), src // np.gcd(src, dst)).astype(np.float32)
    assert got.shape == ref.shape
    assert np.max(np.abs(got - ref)) < 1e-5


def test_streaming_resampler_has_no_chunk_boundary_transient():
    x = tone(1.0, rate=8000, amp=0.5, freq=300.0)
    ref = resample_poly(x, 2, 1)
    rs = StreamingResampler(8000, 16000)
    ours = np.concatenate([rs.process(c) for c in np.split(x, 50)] + [rs.flush()])
    naive = np.concatenate([resample_poly(c, 2, 1) for c in np.split(x, 50)])
    assert np.max(np.abs(ours - ref)) < 1e-5
    # Per-chunk resampling (what we avoid) has large errors at boundaries.
    assert np.max(np.abs(naive - ref)) > 1e-3


def test_chunker_and_timeline():
    ch = Chunker(4)
    assert ch.push(np.arange(3, dtype=np.float32)) == []
    out = ch.push(np.arange(3, 10, dtype=np.float32))
    assert [list(c) for c in out] == [[0, 1, 2, 3], [4, 5, 6, 7]]
    assert list(ch.take_rest()) == [8, 9]
    tl = Timeline(8000, offset_ms=1000)
    tl.advance(4000)
    assert tl.now_ms == 1500
    assert tl.at(800) == 1100


def _ffmpeg(args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_decode_stereo_preserves_channels(tmp_path):
    # left: 440 Hz tone for 1 s then silence; right: silence then tone.
    path = tmp_path / "stereo.wav"
    _ffmpeg(
        [
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1,apad=pad_dur=1",
            "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono:d=1,aformat=sample_rates=8000",
            "-f", "lavfi", "-i", "sine=frequency=660:duration=1",
            "-filter_complex", "[0]aresample=8000[l];[1][2]concat=n=2:v=0:a=1,aresample=8000[r];[l][r]join=inputs=2:channel_layout=stereo",
            "-ar", "8000", str(path),
        ]
    )
    dec = decode_file(str(path), target_rate=16000)
    assert dec.source_channels == 2 and dec.source_sample_rate == 8000 and len(dec.channels) == 2
    left, right = dec.channels
    half = len(left) // 2
    assert np.abs(left[:half]).mean() > 10 * np.abs(left[half:]).mean()
    assert np.abs(right[half:]).mean() > 10 * np.abs(right[:half]).mean()
    mixed = decode_file(str(path), target_rate=16000, mix=True)
    assert len(mixed.channels) == 1


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
@pytest.mark.parametrize("ext,codec", [("mp3", "libmp3lame"), ("flac", "flac"), ("ogg", "libopus")])
def test_decode_compressed_formats(tmp_path, ext, codec):
    path = tmp_path / f"a.{ext}"
    try:
        _ffmpeg(["-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-ar", "48000" if codec == "libopus" else "16000", "-c:a", codec, str(path)])
    except subprocess.CalledProcessError:
        pytest.skip(f"ffmpeg lacks {codec}")
    dec = decode_file(str(path), target_rate=16000)
    assert abs(dec.duration_sec - 2.0) < 0.1
    assert np.abs(dec.channels[0]).max() > 0.1


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_decode_duration_limit(tmp_path):
    path = tmp_path / "long.wav"
    _ffmpeg(["-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono", "-t", "3", str(path)])
    with pytest.raises(DurationExceeded):
        decode_file(str(path), max_seconds=1.0)
