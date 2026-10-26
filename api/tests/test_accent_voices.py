"""Local-accent Fish voices: area-code routing, voice list and preview route.

Uses a small fixture registry in a tmp dir (not the provisioned one) plus the
real area_code_accent_map.json, which is checked in and never regenerated.
"""

import json
import shutil
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api.services.voice_routing import accent_voices
from api.services.voice_routing.accent_voices import (
    DATA_DIR,
    AccentVoiceCatalog,
    list_fish_accent_voices,
    resolve_accent_voice,
)

ACCENT_NAMES = {
    1: "Maine / Down East New England",
    6: "New York City",
    21: "East Tennessee Appalachian / Smoky Mountain",
    33: "New Orleans / Yat-influenced English",
    68: "Colorado / Central Rockies",
    74: "Alaska English",
}


def _vid(accent_id: int, gender: str) -> str:
    return f"{accent_id:02d}{gender[0]}".ljust(32, "0")


def _entry(accent_id: int, genders=("male", "female")) -> dict:
    entry = {
        "accent_id": accent_id,
        "name": ACCENT_NAMES[accent_id],
        "states": "XX",
        "area_codes": ["999"],
    }
    for gender in genders:
        entry[gender] = {"voice_id": _vid(accent_id, gender), "title": "t"}
    return entry


def _write_data_dir(path, registry: dict | None):
    path.mkdir(parents=True, exist_ok=True)
    shutil.copy(DATA_DIR / "area_code_accent_map.json", path)
    (path / "accent_voices.json").write_text(
        json.dumps(
            {
                "accents": [
                    {"accent_id": aid, "name": name}
                    for aid, name in ACCENT_NAMES.items()
                ]
            }
        )
    )
    if registry is not None:
        (path / "accent_voice_registry.json").write_text(json.dumps(registry))
    return path


@pytest.fixture
def catalog(tmp_path):
    registry = {str(aid): _entry(aid) for aid in (6, 21, 33, 68, 74)}
    # Maine has only a male voice, to exercise the gender fallback.
    registry["1"] = _entry(1, genders=("male",))
    data_dir = _write_data_dir(tmp_path / "accent_voices", registry)
    previews = data_dir / "previews"
    previews.mkdir()
    (previews / "21_female.mp3").write_bytes(b"ID3")
    (previews / "06_male.wav").write_bytes(b"RIFF")
    return AccentVoiceCatalog(data_dir)


class TestResolveAccentVoice:
    def test_inbound_uses_caller_number(self, catalog):
        pick = resolve_accent_voice(
            "auto:female",
            {
                "direction": "inbound",
                "caller_number": "+18655551234",
                "called_number": "2125550000",
            },
            catalog=catalog,
        )
        assert pick == {
            "voice_id": _vid(21, "female"),
            "accent_id": 21,
            "accent_name": ACCENT_NAMES[21],
            "gender": "female",
            "area_code": "865",
            "source": "area_code",
        }

    def test_outbound_uses_called_number(self, catalog):
        pick = resolve_accent_voice(
            "auto:male", {"called_number": "2125551234"}, catalog=catalog
        )
        assert pick["accent_id"] == 6
        assert pick["voice_id"] == _vid(6, "male")
        assert pick["source"] == "area_code"

    def test_outbound_legacy_phone_number(self, catalog):
        pick = resolve_accent_voice(
            "auto:male", {"phone_number": "(907) 555-0100"}, catalog=catalog
        )
        assert pick["accent_id"] == 74
        assert pick["area_code"] == "907"

    @pytest.mark.parametrize(
        "context",
        [
            {"called_number": "8005551234"},  # toll-free
            {"called_number": "4165551234"},  # Toronto
            {"called_number": "not a number"},
            {"direction": "inbound", "caller_number": "anonymous"},
            {"direction": "inbound"},
            {},
        ],
    )
    def test_unusable_numbers_use_default_accent(self, catalog, context):
        pick = resolve_accent_voice("auto:female", context, catalog=catalog)
        assert pick["accent_id"] == 68
        assert pick["voice_id"] == _vid(68, "female")
        assert pick["source"] == "default"

    def test_accent_area_code_override(self, catalog):
        pick = resolve_accent_voice(
            "auto:female",
            {"called_number": "2125551234", "accent_area_code": "504"},
            catalog=catalog,
        )
        assert pick["accent_id"] == 33
        assert pick["area_code"] == "504"

    def test_malformed_override_is_ignored(self, catalog):
        pick = resolve_accent_voice(
            "auto:female",
            {"called_number": "2125551234", "accent_area_code": "50"},
            catalog=catalog,
        )
        assert pick["accent_id"] == 6

    @pytest.mark.parametrize("voice", ["", "abc123", _vid(6, "male"), "auto:robot"])
    def test_non_auto_voice_passes_through(self, catalog, voice):
        assert (
            resolve_accent_voice(
                voice, {"called_number": "2125551234"}, catalog=catalog
            )
            is None
        )

    def test_missing_gender_falls_back_to_other_gender(self, catalog):
        pick = resolve_accent_voice(
            "auto:female", {"called_number": "2075551234"}, catalog=catalog
        )
        assert pick["accent_id"] == 1
        assert pick["gender"] == "male"
        assert pick["voice_id"] == _vid(1, "male")
        assert pick["source"] == "area_code"

    def test_unprovisioned_accent_falls_back_to_default(self, catalog):
        # 303 is Colorado itself; 617 (Boston, accent 3) has no voices here.
        pick = resolve_accent_voice(
            "auto:male", {"called_number": "6175551234"}, catalog=catalog
        )
        assert pick["accent_id"] == 68
        assert pick["voice_id"] == _vid(68, "male")
        assert pick["source"] == "default"
        assert pick["area_code"] == "617"

    def test_missing_registry_returns_empty_voice(self, tmp_path):
        empty = AccentVoiceCatalog(_write_data_dir(tmp_path / "empty", None))
        pick = resolve_accent_voice(
            "auto:female", {"called_number": "2125551234"}, catalog=empty
        )
        assert pick["voice_id"] == ""
        assert pick["accent_id"] == 6
        assert pick["accent_name"] == ACCENT_NAMES[6]


class TestRealAreaCodeMap:
    def test_every_code_maps_to_a_known_accent(self):
        amap = json.loads((DATA_DIR / "area_code_accent_map.json").read_text())
        codes = amap["area_code_to_accent_id"]
        assert len(codes) == 372
        assert amap["default_accent_id"] == 68
        for code, accent_id in codes.items():
            assert len(code) == 3 and code.isdigit(), code
            assert 1 <= accent_id <= 75, (code, accent_id)

    def test_spec_has_75_accents(self):
        spec = json.loads((DATA_DIR / "accent_voices.json").read_text())
        assert sorted(a["accent_id"] for a in spec["accents"]) == list(range(1, 76))


class TestListFishAccentVoices:
    def test_auto_entries_first_then_registered_voices(self, catalog):
        voices = list_fish_accent_voices("https://api.example.com/", catalog=catalog)
        assert [v["voice_id"] for v in voices[:2]] == ["auto:female", "auto:male"]
        assert voices[0]["name"] == "Auto — local accent by area code (Female)"
        # 5 accents x 2 genders + Maine male only
        assert len(voices) == 2 + 11
        tn = next(v for v in voices if v["voice_id"] == _vid(21, "female"))
        assert tn["name"] == f"{ACCENT_NAMES[21]} (Female)"
        assert tn["accent"] == ACCENT_NAMES[21]
        assert tn["gender"] == "female"
        assert tn["language"] == "en-US"
        assert tn["description"] == "XX · area codes 999"
        assert tn["preview_url"] == (
            "https://api.example.com/api/v1/public/accent-voices/previews/21_female.mp3"
        )
        nyc = next(v for v in voices if v["voice_id"] == _vid(6, "male"))
        assert nyc["preview_url"].endswith("/previews/06_male.wav")
        no_preview = next(v for v in voices if v["voice_id"] == _vid(68, "male"))
        assert "preview_url" not in no_preview

    def test_filters_keep_auto_entries_pinned(self, catalog):
        voices = list_fish_accent_voices(
            "http://x/", q="tennessee", gender="FEMALE", catalog=catalog
        )
        assert [v["voice_id"] for v in voices] == [
            "auto:female",
            "auto:male",
            _vid(21, "female"),
        ]
        voices = list_fish_accent_voices(
            "http://x/", accent="new york city", catalog=catalog
        )
        assert [v["voice_id"] for v in voices[2:]] == [
            _vid(6, "female"),
            _vid(6, "male"),
        ]

    def test_empty_registry_lists_only_auto(self, tmp_path):
        empty = AccentVoiceCatalog(_write_data_dir(tmp_path / "empty", None))
        voices = list_fish_accent_voices("http://x/", catalog=empty)
        assert [v["voice_id"] for v in voices] == ["auto:female", "auto:male"]


class TestPreviewRoute:
    @pytest.fixture
    def route(self, catalog, monkeypatch):
        from api.routes import public_accent_voices

        monkeypatch.setattr(public_accent_voices, "get_catalog", lambda: catalog)
        return public_accent_voices.get_accent_voice_preview

    @pytest.mark.parametrize(
        "filename",
        ["../x.mp3", "1_male.mp3", "21_female.ogg", "21_other.mp3", "21_male.mp3"],
    )
    async def test_rejects_bad_or_missing_names(self, route, filename):
        with pytest.raises(HTTPException) as exc:
            await route(filename)
        assert exc.value.status_code == 404

    async def test_serves_mp3_with_cache_header(self, route, catalog):
        response = await route("21_female.mp3")
        assert response.media_type == "audio/mpeg"
        assert response.headers["cache-control"] == "public, max-age=86400"
        assert str(response.path) == str(catalog.previews_dir / "21_female.mp3")

    async def test_serves_wav(self, route):
        response = await route("06_male.wav")
        assert response.media_type == "audio/wav"


class TestFishFactoryGuard:
    def test_auto_voice_is_never_sent_to_fish(self, catalog, monkeypatch):
        from api.services.pipecat import service_factory

        monkeypatch.setattr(accent_voices, "_catalog", catalog)
        user_config = SimpleNamespace(
            tts=SimpleNamespace(
                provider="fish",
                api_key="k",
                voice="auto:male",
                model="s2.1-pro",
                latency="balanced",
                speed=1.0,
                volume=0,
                normalize=True,
            )
        )
        audio_config = SimpleNamespace(
            transport_out_sample_rate=8000, transport_in_sample_rate=8000
        )
        service = service_factory.create_tts_service(user_config, audio_config)
        assert service._settings.voice == _vid(68, "male")
