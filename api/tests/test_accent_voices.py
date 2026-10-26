"""Auto local-accent Fish voices: area code -> accent_id -> one Fish clone.

Uses fixture registries (fake Fish ids) next to the real accent dataset and
area-code map; nothing here talks to Fish.
"""

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import public_accent_voices
from api.routes.user import get_voices
from api.schemas.ai_model_configuration import EffectiveAIModelConfiguration
from api.services.configuration.registry import (
    DeepgramTTSConfiguration,
    FishAudioTTSConfiguration,
)
from api.services.pipecat.service_factory import create_tts_service
from api.services.voice_routing import accent_voices
from api.services.voice_routing.accent_voices import (
    ACCENT_REGISTRY,
    AREA_CODE_TO_ACCENT,
    AUTO_LOCAL_ACCENT,
    AUTO_LOCAL_ACCENT_NAME,
    AccentVoiceCatalog,
    accent_runtime_configuration,
    apply_local_accent_voice,
    list_fish_accent_voices,
    resolve_accent_voice,
)
from api.services.voice_routing.blocked_voices import DEFAULT_FISH_VOICE

REAL_DATA_DIR = accent_voices.DATA_DIR

EAST_TN = 21
NYC = 6
NEW_ORLEANS = 33
ALASKA = 74
DEFAULT = 68

FISH_IDS = {
    EAST_TN: "fish0021easttennessee0000000000a",
    NYC: "fish0006newyorkcity0000000000000",
    NEW_ORLEANS: "fish0033neworleans00000000000000",
    ALASKA: "fish0074alaska000000000000000000",
    DEFAULT: "fish0068colorado0000000000000000",
}


def _write_registry(data_dir: Path, voice_ids: dict[int, str]) -> None:
    dataset = json.loads((data_dir / "accents.json").read_text())
    registry = {}
    for acc in dataset["accents"]:
        aid = acc["accent_id"]
        voice_id = voice_ids.get(aid)
        registry[str(aid)] = {
            "accent_id": aid,
            "accent_name": acc["name"],
            "states": acc["states"],
            "area_codes": acc["area_codes"],
            "reference_audio": f"reference_audio/{aid:03d}_reference.wav",
            "fish_voice_id": voice_id,
            "fish_model_title": f"PVN Local Accent {aid:03d} - {acc['name']}",
            "status": "ready" if voice_id else "unavailable",
        }
    (data_dir / "accent_clone_registry.json").write_text(json.dumps(registry))


def _fixture_dir(tmp_path: Path, voice_ids: dict[int, str]) -> Path:
    data_dir = tmp_path / "accent_voices"
    (data_dir / "previews").mkdir(parents=True)
    for name in ("accents.json", "area_code_accent_map.json"):
        shutil.copy(REAL_DATA_DIR / name, data_dir / name)
    _write_registry(data_dir, voice_ids)
    return data_dir


@pytest.fixture
def catalog(tmp_path):
    return AccentVoiceCatalog(_fixture_dir(tmp_path, FISH_IDS))


def _resolve(catalog, context, voice=AUTO_LOCAL_ACCENT):
    return resolve_accent_voice(voice, context, catalog=catalog)


# --- area code routing ------------------------------------------------------


def test_inbound_knoxville_caller_gets_east_tennessee_clone(catalog):
    pick = _resolve(catalog, {"direction": "inbound", "caller_number": "+18655551234"})
    assert pick == {
        "voice_id": FISH_IDS[EAST_TN],
        "accent_id": EAST_TN,
        "accent_name": "East Tennessee Appalachian / Smoky Mountain",
        "area_code": "865",
        "source": "area_code",
    }


@pytest.mark.parametrize("number", ["4235551234", "+1 (729) 555-1234"])
def test_other_east_tennessee_area_codes_share_the_same_clone(catalog, number):
    pick = _resolve(catalog, {"direction": "inbound", "caller_number": number})
    assert pick["accent_id"] == EAST_TN
    assert pick["voice_id"] == FISH_IDS[EAST_TN]
    assert pick["source"] == "area_code"


def test_inbound_uses_the_caller_not_our_did(catalog):
    pick = _resolve(
        catalog,
        {
            "direction": "inbound",
            "caller_number": "8655551234",
            "called_number": "2125550000",  # our own number
        },
    )
    assert pick["accent_id"] == EAST_TN


def test_outbound_nyc_prospect_gets_nyc_clone(catalog):
    pick = _resolve(catalog, {"called_number": "2125551234"})
    assert pick["accent_id"] == NYC
    assert pick["area_code"] == "212"
    assert pick["voice_id"] == FISH_IDS[NYC]
    assert pick["source"] == "area_code"


def test_outbound_ignores_our_caller_id(catalog):
    pick = _resolve(
        catalog,
        {
            "direction": "outbound",
            "caller_number": "8655550000",  # our ANI
            "called_number": "2125551234",
        },
    )
    assert pick["accent_id"] == NYC


def test_outbound_legacy_phone_number(catalog):
    assert _resolve(catalog, {"phone_number": "+12125551234"})["accent_id"] == NYC


def test_alaska(catalog):
    pick = _resolve(catalog, {"called_number": "9075550100"})
    assert pick["accent_id"] == ALASKA
    assert pick["accent_name"] == "Alaska English"
    assert pick["voice_id"] == FISH_IDS[ALASKA]


def test_accent_area_code_override_new_orleans(catalog):
    pick = _resolve(catalog, {"accent_area_code": "504", "called_number": "2125551234"})
    assert pick["accent_id"] == NEW_ORLEANS
    assert pick["accent_name"] == "New Orleans / Yat-influenced English"
    assert pick["area_code"] == "504"
    assert pick["source"] == "area_code"


@pytest.mark.parametrize("override", ["50", "5041", "abc", ""])
def test_invalid_accent_area_code_override_is_ignored(catalog, override):
    pick = _resolve(
        catalog, {"accent_area_code": override, "called_number": "2125551234"}
    )
    assert pick["accent_id"] == NYC


# --- fallback ---------------------------------------------------------------


@pytest.mark.parametrize(
    "context",
    [
        {"called_number": "8005551234"},  # toll-free
        {"direction": "inbound", "caller_number": "+18885551234"},  # toll-free
        {"called_number": "garbage"},
        {"called_number": ""},
        {},  # missing number
        {"direction": "inbound"},  # inbound without caller
        {"called_number": "+442071234567"},  # UK
        {"called_number": "4165551234"},  # Toronto: NANP but not in the US map
        {"called_number": "0125551234"},  # not a valid NANP area code
        {"called_number": "4115551234"},  # N11 service code
    ],
)
def test_unusable_geography_falls_back_to_default_accent(catalog, context):
    pick = _resolve(catalog, context)
    assert pick["accent_id"] == DEFAULT
    assert pick["accent_name"] == "Colorado / Central Rockies"
    assert pick["voice_id"] == FISH_IDS[DEFAULT]
    assert pick["source"] == "default"


def test_missing_number_has_no_area_code(catalog):
    assert _resolve(catalog, {})["area_code"] is None
    assert _resolve(catalog, {"called_number": "8005551234"})["area_code"] is None


def test_mapped_accent_without_a_clone_uses_default(catalog):
    # 207 (Maine, accent 1) is mapped but has no clone in the fixture.
    pick = _resolve(catalog, {"called_number": "2075551234"})
    assert pick["accent_id"] == DEFAULT
    assert pick["voice_id"] == FISH_IDS[DEFAULT]
    assert pick["area_code"] == "207"
    assert pick["source"] == "default"


def test_default_accent_without_a_clone_uses_standard_voice(tmp_path):
    ids = {k: v for k, v in FISH_IDS.items() if k != DEFAULT}
    catalog = AccentVoiceCatalog(_fixture_dir(tmp_path, ids))
    assert _resolve(catalog, {"called_number": "garbage"}) == {
        "voice_id": DEFAULT_FISH_VOICE,
        "accent_id": None,
        "accent_name": "",
        "area_code": None,
        "source": "standard_voice",
    }
    # A provisioned accent still routes normally.
    assert _resolve(catalog, {"called_number": "2125551234"})["accent_id"] == NYC


def test_entries_that_are_not_ready_are_never_used(tmp_path):
    data_dir = _fixture_dir(tmp_path, FISH_IDS)
    registry = json.loads((data_dir / "accent_clone_registry.json").read_text())
    registry[str(EAST_TN)]["status"] = "failed"
    (data_dir / "accent_clone_registry.json").write_text(json.dumps(registry))
    catalog = AccentVoiceCatalog(data_dir)
    assert _resolve(catalog, {"called_number": "8655551234"})["accent_id"] == DEFAULT


def test_missing_registry_never_crashes(tmp_path):
    catalog = AccentVoiceCatalog(tmp_path / "does-not-exist")
    assert catalog.registry == {}
    pick = _resolve(catalog, {"called_number": "8655551234"})
    assert pick["voice_id"] == DEFAULT_FISH_VOICE
    assert pick["source"] == "standard_voice"
    assert list_fish_accent_voices("http://x/", catalog=catalog)[0]["voice_id"] == (
        AUTO_LOCAL_ACCENT
    )


def test_corrupt_registry_never_crashes(tmp_path):
    data_dir = _fixture_dir(tmp_path, FISH_IDS)
    (data_dir / "accent_clone_registry.json").write_text("{not json")
    catalog = AccentVoiceCatalog(data_dir)
    assert _resolve(catalog, {"called_number": "8655551234"})["source"] == (
        "standard_voice"
    )


@pytest.mark.parametrize(
    "context",
    [None, {"called_number": 2125551234}, {"called_number": ["x"]}, {"direction": 5}],
)
def test_resolver_never_raises_on_bad_data(catalog, context):
    assert _resolve(catalog, context)["voice_id"]


# --- passthrough ------------------------------------------------------------


@pytest.mark.parametrize(
    "voice", ["xyz123", FISH_IDS[NYC], DEFAULT_FISH_VOICE, "", "auto:female"]
)
def test_fixed_voices_pass_through(catalog, voice):
    assert _resolve(catalog, {"called_number": "8655551234"}, voice=voice) is None


# --- shipped data -----------------------------------------------------------


def test_module_level_lookups_use_the_shipped_map():
    assert AREA_CODE_TO_ACCENT["865"] == EAST_TN
    assert AREA_CODE_TO_ACCENT["423"] == EAST_TN
    assert AREA_CODE_TO_ACCENT["729"] == EAST_TN
    assert AREA_CODE_TO_ACCENT["212"] == NYC
    assert set(ACCENT_REGISTRY) == set(range(1, 76))


def test_every_area_code_maps_to_exactly_one_valid_accent():
    dataset = json.loads((REAL_DATA_DIR / "accents.json").read_text())
    amap = json.loads((REAL_DATA_DIR / "area_code_accent_map.json").read_text())
    accent_ids = {a["accent_id"] for a in dataset["accents"]}
    assert accent_ids == set(range(1, 76))
    assert amap["default_accent_id"] == DEFAULT == dataset["default_accent_id"]

    owners: dict[str, list[int]] = {}
    for acc in dataset["accents"]:
        assert acc["area_codes"], f"accent {acc['accent_id']} has no area codes"
        for code in acc["area_codes"]:
            owners.setdefault(code, []).append(acc["accent_id"])
    assert all(len(ids) == 1 for ids in owners.values())

    mapping = amap["area_code_to_accent_id"]
    assert set(mapping) == set(owners)
    for code, aid in mapping.items():
        assert len(code) == 3 and code.isdigit()
        assert aid in accent_ids
        assert owners[code] == [aid]


def test_shipped_registry_and_routing_map_are_consistent():
    registry = json.loads((REAL_DATA_DIR / "accent_clone_registry.json").read_text())
    routing = json.loads((REAL_DATA_DIR / "accent_routing_map.json").read_text())
    amap = json.loads((REAL_DATA_DIR / "area_code_accent_map.json").read_text())
    assert set(registry) == {str(i) for i in range(1, 76)}
    assert set(routing) == set(amap["area_code_to_accent_id"])
    for code, entry in routing.items():
        reg = registry[str(entry["accent_id"])]
        expected = reg["fish_voice_id"] if reg["status"] == "ready" else None
        assert entry["fish_voice_id"] == expected
        assert entry["accent_id"] == amap["area_code_to_accent_id"][code]

    # One clone per accent: a Fish id is never shared between accents, and
    # every area code of an accent points at that accent's single id.
    ready = [e["fish_voice_id"] for e in registry.values() if e["fish_voice_id"]]
    assert len(ready) == len(set(ready))


def test_reference_audio_is_excluded_from_image_and_git():
    repo = REAL_DATA_DIR.parents[2]
    assert "api/data/accent_voices/reference_audio/" in (
        (repo / ".dockerignore").read_text().splitlines()
    )
    gitignore = (REAL_DATA_DIR / "reference_audio" / ".gitignore").read_text()
    assert "*" in gitignore.splitlines()


# --- voice list -------------------------------------------------------------


def test_voice_list_pins_auto_then_lists_provisioned_clones(catalog):
    (catalog.previews_dir / "021.mp3").write_bytes(b"ID3")
    catalog = AccentVoiceCatalog(catalog.data_dir)  # pick up the preview
    voices = list_fish_accent_voices("http://api.test", catalog=catalog)

    assert voices[0]["voice_id"] == AUTO_LOCAL_ACCENT
    assert voices[0]["name"] == AUTO_LOCAL_ACCENT_NAME
    assert [v["voice_id"] for v in voices[1:]] == [
        FISH_IDS[aid] for aid in sorted(FISH_IDS)
    ]
    east_tn = next(v for v in voices if v["voice_id"] == FISH_IDS[EAST_TN])
    assert east_tn == {
        "voice_id": FISH_IDS[EAST_TN],
        "name": "East Tennessee Appalachian / Smoky Mountain",
        "accent": "East Tennessee Appalachian / Smoky Mountain",
        "language": "en-US",
        "description": "TN · area codes 423, 729, 865",
        "preview_url": "http://api.test/api/v1/public/accent-voices/previews/021.mp3",
    }
    nyc = next(v for v in voices if v["voice_id"] == FISH_IDS[NYC])
    assert "preview_url" not in nyc


def test_voice_list_filters_keep_auto_pinned(catalog):
    voices = list_fish_accent_voices("http://x/", q="alaska", catalog=catalog)
    assert [v["voice_id"] for v in voices] == [AUTO_LOCAL_ACCENT, FISH_IDS[ALASKA]]
    voices = list_fish_accent_voices(
        "http://x/", accent="new york city", catalog=catalog
    )
    assert [v["voice_id"] for v in voices] == [AUTO_LOCAL_ACCENT, FISH_IDS[NYC]]


async def test_voices_endpoint_serves_fish_from_registry(catalog, monkeypatch):
    monkeypatch.setattr(accent_voices, "_catalog", catalog)
    response = await get_voices(
        "fish",
        request=SimpleNamespace(base_url="http://api.test/"),
        user=SimpleNamespace(),
    )
    assert response.provider == "fish"
    assert response.voices[0].voice_id == AUTO_LOCAL_ACCENT
    assert len(response.voices) == 1 + len(FISH_IDS)
    assert "Alaska English" in response.facets.accents


# --- call pipeline ----------------------------------------------------------


def _effective_config(voice):
    return EffectiveAIModelConfiguration(
        tts=FishAudioTTSConfiguration(api_key="test-key", voice=voice)
    )


def test_apply_swaps_auto_for_the_clone_and_records_it(catalog):
    config = _effective_config(AUTO_LOCAL_ACCENT)
    context = {"direction": "inbound", "caller_number": "+18655551234"}

    new_config, pick = apply_local_accent_voice(
        config, context, workflow_run_id=7, catalog=catalog
    )

    assert new_config.tts.voice == FISH_IDS[EAST_TN]
    assert config.tts.voice == AUTO_LOCAL_ACCENT  # the shared config is untouched
    assert context["accent_voice"] == {
        "voice_id": FISH_IDS[EAST_TN],
        "accent_id": EAST_TN,
        "accent_name": "East Tennessee Appalachian / Smoky Mountain",
        "area_code": "865",
        "source": "area_code",
    }
    assert context["accent_name"] == "East Tennessee Appalachian / Smoky Mountain"
    assert accent_runtime_configuration(pick) == {
        "accent_id": EAST_TN,
        "accent_name": "East Tennessee Appalachian / Smoky Mountain",
        "accent_area_code": "865",
        "accent_source": "area_code",
    }


def test_apply_leaves_fixed_and_non_fish_voices_alone(catalog):
    context = {"called_number": "2125551234"}
    fixed = _effective_config("xyz123")
    assert apply_local_accent_voice(fixed, context, catalog=catalog) == (fixed, None)
    other = EffectiveAIModelConfiguration(
        tts=DeepgramTTSConfiguration(api_key="k", voice=AUTO_LOCAL_ACCENT)
    )
    assert apply_local_accent_voice(other, context, catalog=catalog) == (other, None)
    assert "accent_voice" not in context
    assert accent_runtime_configuration(None) == {}


def test_apply_never_logs_the_full_number(catalog):
    from loguru import logger

    messages = []
    handler = logger.add(messages.append, format="{message}")
    try:
        apply_local_accent_voice(
            _effective_config(AUTO_LOCAL_ACCENT),
            {"direction": "inbound", "caller_number": "+18655551234"},
            workflow_run_id=42,
            catalog=catalog,
        )
    finally:
        logger.remove(handler)
    text = "".join(messages)
    assert "Accent voice resolved" in text and "area_code=865" in text
    assert "5551234" not in text


def test_auto_value_is_a_valid_saved_fish_voice():
    assert FishAudioTTSConfiguration(api_key="k", voice=AUTO_LOCAL_ACCENT).voice == (
        AUTO_LOCAL_ACCENT
    )


def test_factory_never_sends_the_auto_value_to_fish(catalog, monkeypatch):
    monkeypatch.setattr(accent_voices, "_catalog", catalog)
    user_config = SimpleNamespace(
        tts=SimpleNamespace(
            provider="fish",
            api_key="test-key",
            model="s2.1-pro",
            voice=AUTO_LOCAL_ACCENT,
            latency="balanced",
            speed=None,
            volume=None,
            normalize=True,
        )
    )
    audio_config = SimpleNamespace(
        transport_out_sample_rate=8000, transport_in_sample_rate=8000
    )
    service = create_tts_service(user_config, audio_config)
    assert service._settings.voice == FISH_IDS[DEFAULT]


# --- preview route ----------------------------------------------------------


@pytest.fixture
def preview_client(catalog, monkeypatch):
    (catalog.previews_dir / "021.mp3").write_bytes(b"ID3fake-mp3")
    (catalog.data_dir / "reference_audio").mkdir()
    (catalog.data_dir / "reference_audio" / "021_reference.wav").write_bytes(b"RIFF")
    monkeypatch.setattr(public_accent_voices, "get_catalog", lambda: catalog)
    app = FastAPI()
    app.include_router(public_accent_voices.router, prefix="/api/v1")
    return TestClient(app)


def test_preview_is_served_with_cache_headers(preview_client):
    r = preview_client.get("/api/v1/public/accent-voices/previews/021.mp3")
    assert r.status_code == 200
    assert r.content == b"ID3fake-mp3"
    assert r.headers["content-type"] == "audio/mpeg"
    assert r.headers["cache-control"] == "public, max-age=86400"


@pytest.mark.parametrize(
    "filename",
    [
        "022.mp3",  # well-formed but missing
        "21.mp3",
        "0021.mp3",
        "021.wav",
        "021.mp3x",
        "..%2F..%2Faccent_clone_registry.json",
        "..%2Freference_audio%2F021_reference.wav",
        "021_reference.wav",
        "%2E%2E",
    ],
)
def test_preview_rejects_unexpected_filenames(preview_client, filename):
    r = preview_client.get(f"/api/v1/public/accent-voices/previews/{filename}")
    assert r.status_code == 404
