"""set_override_env.py: env-only edits of the box's api override (offline)."""

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "deploy" / "hopwhistle" / "cutover" / "set_override_env.py"

MAP_STYLE = """\
# box override
services:
  api:
    image: hopwhistle/dograh-api:abcd1234
    volumes:
      - /opt/dograh-patches/pvn-stir:/app/certs:ro
    environment:
      CALL_HYGIENE_ENABLED: "true"   # kept
      HOPSTT_ENABLED: "true"
  ui:
    image: dograh-ui:voicestudio
"""

LIST_STYLE = """\
services:
  api:
    image: hopwhistle/dograh-api:abcd1234
    environment:
      - "CALL_HYGIENE_ENABLED=true"
"""


def _run(tmp_path, text, values):
    path = tmp_path / "docker-compose.override.yaml"
    path.write_text(text)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(path)],
        input=json.dumps(values),
        capture_output=True,
        text=True,
    )
    return proc, path.read_text()


def test_map_style_adds_and_replaces_without_touching_other_lines(tmp_path):
    secret = "aai-secret-value-123456"
    proc, out = _run(
        tmp_path,
        MAP_STYLE,
        {"ASSEMBLYAI_API_KEY": secret, "HOPSTT_ENABLED": "false"},
    )
    assert proc.returncode == 0, proc.stderr
    assert secret not in proc.stdout + proc.stderr  # names only
    assert f'      ASSEMBLYAI_API_KEY: "{secret}"' in out
    assert '      HOPSTT_ENABLED: "false"' in out
    assert 'CALL_HYGIENE_ENABLED: "true"   # kept' in out
    assert "image: hopwhistle/dograh-api:abcd1234" in out
    assert "/opt/dograh-patches/pvn-stir:/app/certs:ro" in out
    assert "image: dograh-ui:voicestudio" in out


def test_list_style_is_kept_as_list(tmp_path):
    proc, out = _run(tmp_path, LIST_STYLE, {"ASSEMBLYAI_STT_ROLLOUT": "canary"})
    assert proc.returncode == 0, proc.stderr
    assert '      - "ASSEMBLYAI_STT_ROLLOUT=canary"' in out
    assert '      - "CALL_HYGIENE_ENABLED=true"' in out


def test_refuses_bad_names_and_multiline_values(tmp_path):
    proc, out = _run(tmp_path, MAP_STYLE, {"bad name": "x"})
    assert proc.returncode == 2 and out == MAP_STYLE
    proc, out = _run(tmp_path, MAP_STYLE, {"OK_NAME": "a\nb"})
    assert proc.returncode == 2 and out == MAP_STYLE
