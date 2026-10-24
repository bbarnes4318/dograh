import numpy as np

from hopvoice.audio import float_to_pcm_s16le, pcm_chunks, resample


def test_pcm_is_little_endian_16_bit_and_clipped():
    raw = float_to_pcm_s16le(np.array([-2.0, -1.0, 0.0, 1.0, 2.0], dtype=np.float32))
    values = np.frombuffer(raw, dtype="<i2")
    assert values.tolist() == [-32767, -32767, 0, 32767, 32767]


def test_resample_preserves_expected_duration():
    audio = np.zeros(24000, dtype=np.float32)
    result = resample(audio, 24000, 8000)
    assert abs(len(result) - 8000) <= 1


def test_pcm_chunks_align_to_samples():
    pcm = b"\x00\x00" * 1000
    chunks = list(pcm_chunks(pcm, sample_rate=8000, chunk_ms=20))
    assert all(len(chunk) % 2 == 0 for chunk in chunks)
    assert b"".join(chunks) == pcm
