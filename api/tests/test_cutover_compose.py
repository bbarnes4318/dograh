"""docker-compose.override.yaml parsing and rewriting for the cutover."""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "deploy" / "hopwhistle" / "cutover"))

from cutover_lib import compose

OVERRIDE = """\
# box override
services:
  api:
    image: dograhai/dograh-api:latest
    volumes:
      - /opt/dograh-patches/provider.py:/app/api/services/telephony/providers/ari/provider.py:ro
      - "/opt/dograh-patches/reports.py:/app/api/routes/reports.py:ro"
      - ./logs:/app/logs
    environment:
      FISH_API_KEY: "abc"   # kept
      DOGRAH_STATE_CID_POLICY: prefer
  ui:
    image: dograh-ui:voicestudio
volumes:
  data: {}
"""


def test_parse_reads_image_volumes_env():
    parsed = compose.parse(OVERRIDE)
    api = parsed.services["api"]
    assert api.image == "dograhai/dograh-api:latest"
    assert [v.target for v in api.volumes] == [
        "/app/api/services/telephony/providers/ari/provider.py",
        "/app/api/routes/reports.py",
        "/app/logs",
    ]
    assert api.volumes[0].mode == "ro" and api.volumes[2].is_bind
    assert api.env == {"FISH_API_KEY": "abc", "DOGRAH_STATE_CID_POLICY": "prefer"}
    assert not api.unsupported
    assert parsed.services["ui"].image == "dograh-ui:voicestudio"


def test_rewrite_removes_only_selected_mounts_and_keeps_everything_else():
    parsed = compose.parse(OVERRIDE)
    out = compose.rewrite_service(
        parsed,
        "api",
        "hopwhistle/dograh-api:abc12345",
        {
            "/app/api/services/telephony/providers/ari/provider.py",
            "/app/api/routes/reports.py",
        },
        {"ARI_PJSIP_DEFAULT_TRUNK": "fractel", "DOGRAH_STATE_CID_POLICY": "strict"},
    )
    again = compose.parse(out)
    api = again.services["api"]
    assert api.image == "hopwhistle/dograh-api:abc12345"
    assert [v.target for v in api.volumes] == ["/app/logs"]
    assert api.env == {
        "FISH_API_KEY": "abc",
        "DOGRAH_STATE_CID_POLICY": "strict",
        "ARI_PJSIP_DEFAULT_TRUNK": "fractel",
    }
    assert (
        "# box override" in out and "dograh-ui:voicestudio" in out and "data: {}" in out
    )


def test_rewrite_drops_emptied_volumes_key_and_adds_environment_block():
    text = "services:\n  api:\n    volumes:\n      - /opt/a.py:/app/api/a.py:ro\n"
    out = compose.rewrite_service(
        compose.parse(text), "api", "img:1", {"/app/api/a.py"}, {"X": "1"}
    )
    assert "volumes:" not in out
    api = compose.parse(out).services["api"]
    assert api.image == "img:1" and api.env == {"X": "1"} and api.volumes == []


def test_list_style_environment_is_preserved():
    text = "services:\n  api:\n    image: a\n    environment:\n      - A=1\n"
    out = compose.rewrite_service(
        compose.parse(text), "api", "b", set(), {"A": "2", "B": "3"}
    )
    api = compose.parse(out).services["api"]
    assert api.env_style == "list" and api.env == {"A": "2", "B": "3"}


def test_values_are_quoted_safely():
    text = "services:\n  api:\n    image: a\n"
    out = compose.rewrite_service(
        compose.parse(text), "api", "b", set(), {"A": 'x"y\\z'}
    )
    assert (
        compose.parse(out).services["api"].env["A"] == 'x\\"y\\\\z'
    )  # raw YAML text, still one line


@pytest.mark.parametrize(
    "text,fragment",
    [
        (
            "services:\n  api:\n    volumes:\n      - type: bind\n        source: /a\n        target: /b\n",
            "volume",
        ),
        ('services:\n  api:\n    volumes: ["/a:/b"]\n', "inline volumes"),
        ("services:\n  api:\n    environment: {A: 1}\n", "inline environment"),
        ("services:\n  api:\n   image: a\n", "unexpected indentation"),
    ],
)
def test_unsupported_syntax_is_reported_not_guessed(text, fragment):
    api = compose.parse(text).services["api"]
    assert any(fragment in u for u in api.unsupported)
    with pytest.raises(compose.OverrideError):
        compose.rewrite_service(compose.parse(text), "api", "b", set(), {})


def test_tabs_and_missing_services_are_errors():
    with pytest.raises(compose.OverrideError):
        compose.parse("services:\n\tapi:\n")
    with pytest.raises(compose.OverrideError):
        compose.parse("version: '3'\n")
