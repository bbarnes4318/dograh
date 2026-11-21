import pytest

from conftest import make_settings
from hopstt.config import ConfigError, Settings
from hopstt.models import CATALOG


def test_refuses_missing_or_short_key():
    with pytest.raises(ConfigError):
        make_settings(api_key=None).validate()
    with pytest.raises(ConfigError):
        make_settings(api_key="short").validate()
    make_settings(api_key=None, dev_insecure=True).validate()


def test_rejects_unknown_models_and_bad_defaults():
    with pytest.raises(ConfigError):
        make_settings(allowed_models=("stub-streaming", "nope")).validate()
    with pytest.raises(ConfigError):
        make_settings(default_streaming_model="nemotron-3.5-streaming").validate()
    with pytest.raises(ConfigError):
        make_settings(default_batch_model="stub-streaming").validate()
    with pytest.raises(ConfigError):
        make_settings(role="sideways").validate()
    # a live-only worker does not need a batch default
    make_settings(role="live", allowed_models=("stub-streaming",), default_batch_model="x").validate()


def test_from_env(monkeypatch):
    monkeypatch.setenv("HOPSTT_ROLE", "live")
    monkeypatch.setenv("HOPSTT_API_KEY", "k" * 32)
    monkeypatch.setenv("HOPSTT_ALLOWED_MODELS", "nemotron-3.5-streaming, nemotron-en-streaming")
    monkeypatch.setenv("HOPSTT_MAX_STREAMS", "120")
    monkeypatch.setenv("HOPSTT_MODEL_REVISIONS", "nemotron-3.5-streaming=abc123")
    s = Settings.from_env()
    s.validate()
    assert s.role == "live" and s.max_streams == 120
    assert s.allowed_models == ("nemotron-3.5-streaming", "nemotron-en-streaming")
    assert s.spec("nemotron-3.5-streaming").revision == "abc123"
    assert s.spec("nemotron-en-streaming").revision == CATALOG["nemotron-en-streaming"].revision
    assert [m.id for m in s.models_to_load()] == ["nemotron-3.5-streaming", "nemotron-en-streaming"]


def test_catalog_pins_every_downloadable_model():
    for spec in CATALOG.values():
        if spec.hf_repo:
            assert spec.revision and len(spec.revision) == 40, spec.id
        if spec.kind == "streaming":
            assert spec.default_chunk_ms in spec.chunk_ms
    n35 = CATALOG["nemotron-3.5-streaming"]
    assert n35.chunk_ms == (80, 160, 320, 560, 1120)
    assert n35.options["right_context"] == {80: 0, 160: 1, 320: 3, 560: 6, 1120: 13}
    assert n35.options["left_context"] == 56
    en = CATALOG["nemotron-en-streaming"]
    assert en.options["left_context"] == 70 and set(en.options["right_context"].values()) == {0, 1, 6, 13}
