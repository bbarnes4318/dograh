"""deploy/hopwhistle/apply_mercury_voice_patch.py: guards and idempotency.

The script edits the patch files bind-mounted into the production api
container, so it must write nothing unless every anchor matches, and must not
run before the Inception and Mercury 3 patches it builds on.
"""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "hopwhistle"
    / "apply_mercury_voice_patch.py"
)


@pytest.fixture(scope="module")
def patch_module():
    spec = importlib.util.spec_from_file_location("apply_mercury_voice_patch", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _box(tmp_path, patch_module, *, mounts=True, prerequisites=True):
    """A fake box: compose dir plus patch files that contain every anchor."""
    compose = tmp_path / "compose"
    patches = tmp_path / "patches"
    compose.mkdir()
    patches.mkdir()
    if mounts:
        (compose / "docker-compose.override.yaml").write_text(
            "volumes:\n  - /opt/dograh-patches/registry.py:/app/x.py\n"
        )
    for name, edits in patch_module.EDITS.items():
        marker, _ = patch_module.PREREQUISITES[name]
        parts = [f"# {marker}\n"] if prerequisites else []
        parts += [find for find, _ in edits]
        (patches / name).write_text("\n# --\n".join(parts))
    return compose, patches


def _run(patch_module, compose, patches, *extra):
    return patch_module.main(
        ["--patch-dir", str(patches), "--compose-dir", str(compose), *extra]
    )


def _snapshot(patches):
    return {p.name: p.read_text() for p in sorted(patches.iterdir())}


def test_applies_every_edit_then_is_idempotent(tmp_path, patch_module):
    compose, patches = _box(tmp_path, patch_module)

    assert _run(patch_module, compose, patches) == 0
    for name, marker in patch_module.IDEMPOTENCY_MARKERS.items():
        assert marker in (patches / name).read_text()
    assert (
        "temperature=INCEPTION_MIN_TEMPERATURE"
        in (patches / "service_factory.py").read_text()
    )

    after_first = {
        name: text for name, text in _snapshot(patches).items() if ".bak-" not in name
    }
    assert _run(patch_module, compose, patches) == 0
    after_second = {
        name: text for name, text in _snapshot(patches).items() if ".bak-" not in name
    }
    assert after_second == after_first


def test_dry_run_writes_nothing(tmp_path, patch_module):
    compose, patches = _box(tmp_path, patch_module)
    before = _snapshot(patches)
    assert _run(patch_module, compose, patches, "--dry-run") == 0
    assert _snapshot(patches) == before


def test_refuses_before_the_earlier_patches(tmp_path, patch_module, capsys):
    compose, patches = _box(tmp_path, patch_module, prerequisites=False)
    before = _snapshot(patches)
    assert _run(patch_module, compose, patches) == 1
    assert _snapshot(patches) == before
    assert "run apply_mercury3_patch.py first" in capsys.readouterr().out


def test_missing_anchor_blocks_every_file(tmp_path, patch_module):
    compose, patches = _box(tmp_path, patch_module)
    registry = patches / "registry.py"
    registry.write_text(registry.read_text().replace("INCEPTION_MODELS", "X"))
    before = _snapshot(patches)
    assert _run(patch_module, compose, patches) == 1
    assert _snapshot(patches) == before


def test_fork_image_box_is_left_alone(tmp_path, patch_module):
    compose, patches = _box(tmp_path, patch_module, mounts=False)
    before = _snapshot(patches)
    assert _run(patch_module, compose, patches) == 0
    assert _snapshot(patches) == before
