import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _load_patch_module():
    path = REPO / "deploy" / "hopwhistle" / "apply_call_duration_patch.py"
    spec = importlib.util.spec_from_file_location("apply_call_duration_patch", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_embedded_sources_match_repo():
    patch = _load_patch_module()
    helper = REPO / "api" / "services" / "workflow" / "call_duration.py"
    backfill = REPO / "scripts" / "backfill_call_durations.py"
    assert patch.CALL_DURATION_SOURCE == helper.read_text()
    assert patch.BACKFILL_SOURCE == backfill.read_text()


def test_client_edits_are_what_the_repo_client_contains():
    patch = _load_patch_module()
    client = (REPO / "api" / "db" / "workflow_run_client.py").read_text()
    for _, replacement in patch.CLIENT_EDITS[1:]:
        assert replacement in client
