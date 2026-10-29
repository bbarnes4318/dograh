"""Mount compatibility: a patch mount is only dropped when its behavior is
known, present in the target image and, where patch-only, re-created by
explicit settings."""

import hashlib
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CUTOVER = REPO / "deploy" / "hopwhistle" / "cutover"
sys.path.insert(0, str(CUTOVER))

from cutover_lib.compose import Volume
from cutover_lib.mounts import evaluate, load_manifest

MANIFEST = load_manifest(str(CUTOVER / "mount_manifest.json"))
PROVIDER = "/app/api/services/telephony/providers/ari/provider.py"
DISPATCHER = "/app/api/services/campaign/campaign_call_dispatcher.py"


def source_tree_as_image():
    """Every path the manifest may check, read from this checkout, standing in
    for an image built from it."""
    paths = set(MANIFEST["mounts"])
    for b in MANIFEST["behaviors"].values():
        paths |= {e["path"] for e in b["evidence"]}
    out = {}
    for path in paths:
        local = REPO / path.removeprefix("/app/")
        out[path] = local.read_text() if local.exists() else None
    return out


IMAGE = source_tree_as_image()

# Stand-ins for the files the kits produce. Only the parts the manifest keys
# on matter; the phone numbers are fictional.
STOCK_PROVIDER = """
def x(to_number, destination, kwargs):
    if to_number.startswith("SIP/") or to_number.startswith("PJSIP/"):
        pass
    a = f"PJSIP/{to_number}@fractel"
    b = f"PJSIP/{destination}@fractel"
    caller = kwargs.get("caller_id") or "+15550009999"
"""
V3_PROVIDER = STOCK_PROVIDER.replace('@fractel"', '@{_hw_ari_trunk()}"') + (
    "\n# HOPWHISTLE_ARI_TRUNK_V1\n# HOPWHISTLE_ARI_DIAL_PREFIX_V2\n# HOPWHISTLE_ARI_DIAL_FORMAT_V3\n"
)
CALLER_TRUNK_PROVIDER = """
def _pjsip_endpoint(number):
    trunk = os.environ.get("ARI_PJSIP_DEFAULT_TRUNK", "")
# HOPWHISTLE_ARI_CALLER_TRUNK_V1
transfer_caller_id = os.environ.get("ARI_TRANSFER_DEFAULT_CALLER_ID", "")
"""
KIT_DISPATCHER = """
NO_CALLER_ID_FOR_STATE_REASON = "NO_CALLER_ID_AVAILABLE_FOR_DESTINATION_STATE"
SHARED_TRANSFER_DID = os.environ.get("DOGRAH_TRANSFER_DESTINATION") or "+15550001234"
"""
BOX_DISPATCHER = """
            initial_context = {
                "transfer_destination": (
                    queued_run.context_variables.get("transfer_destination")
                    or campaign.orchestrator_metadata.get("transfer_destination")
                    or "+15550004321"
                ),
            }
"""


def run(files, env=None, acks=None, operator=None, image=None):
    host = {f"/opt/dograh-patches/{i}.py": text for i, (_, text) in enumerate(files)}
    mounts = [
        Volume(
            raw="",
            source=f"/opt/dograh-patches/{i}.py",
            target=target,
            mode="ro",
            line=i,
        )
        for i, (target, _) in enumerate(files)
    ]
    return evaluate(
        MANIFEST,
        mounts,
        host.get,
        image or IMAGE,
        env or {},
        acks or {},
        operator or {},
    )


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def test_current_source_carries_every_ported_behavior():
    # Every behavior marked in_source/config has its evidence in this checkout.
    for name, behavior in MANIFEST["behaviors"].items():
        if behavior["status"] == "unported":
            continue
        for ev in behavior["evidence"]:
            body = IMAGE[ev["path"]]
            assert body is not None, f"{name}: {ev['path']} missing from source"
            import re

            assert re.search(ev["pattern"], body, flags=re.MULTILINE), (
                f"{name}: no {ev['pattern']} in {ev['path']}"
            )


def test_v3_trunk_patch_is_preserved_through_settings():
    res = run(
        [(PROVIDER, V3_PROVIDER)],
        env={
            "DOGRAH_ARI_TRUNK": "anveo",
            "DOGRAH_ARI_DIAL_PREFIX": "012345",
            "DOGRAH_ARI_DIAL_FORMAT": "nanp11",
        },
        acks={PROVIDER: sha(V3_PROVIDER)},
    )
    assert not res.blockers, res.blockers
    assert res.results[0].action == "remove"
    assert res.env == {
        "ARI_PJSIP_DEFAULT_TRUNK": "anveo",
        "ARI_DIAL_ANY_TECHNOLOGY": "true",
        "ARI_DIAL_PREFIX": "012345",
        "ARI_DIAL_FORMAT": "nanp11",
        "ARI_TRANSFER_DEFAULT_CALLER_ID": "+15550009999",
    }
    assert any(
        "DOGRAH_ARI_TRUNK is set but the target image ignores it" in w
        for w in res.warnings
    )


def test_v1_patch_without_env_keeps_patch_default_trunk():
    text = V3_PROVIDER.replace(
        'kwargs.get("caller_id") or "+15550009999"',
        'os.environ["ARI_TRANSFER_DEFAULT_CALLER_ID"]',
    )
    res = run([(PROVIDER, text)])
    assert not res.blockers, res.blockers
    assert (
        res.env["ARI_PJSIP_DEFAULT_TRUNK"] == "fractel"
    )  # the patch's default, now explicit


def test_heuristic_variant_requires_acknowledgement():
    res = run([(PROVIDER, STOCK_PROVIDER)])
    assert res.results[0].action == "block"
    assert any("--ack-mount" in b and sha(STOCK_PROVIDER) in b for b in res.blockers)
    ok = run([(PROVIDER, STOCK_PROVIDER)], acks={PROVIDER: sha(STOCK_PROVIDER)})
    assert not ok.blockers
    assert ok.env["ARI_PJSIP_DEFAULT_TRUNK"] == "fractel"
    # Acknowledging a different file does not count.
    stale = run([(PROVIDER, STOCK_PROVIDER)], acks={PROVIDER: sha("something else")})
    assert stale.blockers


def test_caller_trunk_patch_on_fork_provider():
    res = run(
        [(PROVIDER, CALLER_TRUNK_PROVIDER)],
        env={"DOGRAH_ARI_CALLER_TRUNKS": "vonage/nanp11:+15550001111"},
    )
    assert not res.blockers, res.blockers
    assert res.env == {"ARI_CALLER_TRUNKS": "vonage/nanp11:+15550001111"}


def test_forced_shared_did_becomes_explicit_policy():
    res = run([(DISPATCHER, KIT_DISPATCHER)])
    assert not res.blockers, res.blockers
    assert res.env == {
        "CAMPAIGN_TRANSFER_DESTINATION_POLICY": "forced_default",
        "CAMPAIGN_DEFAULT_TRANSFER_DESTINATION": "+15550001234",
    }
    from_env = run(
        [(DISPATCHER, KIT_DISPATCHER)],
        env={"DOGRAH_TRANSFER_DESTINATION": "+15550005555"},
    )
    assert from_env.env["CAMPAIGN_DEFAULT_TRANSFER_DESTINATION"] == "+15550005555"


def test_box_dispatcher_fallback_literal_needs_review_then_context_first():
    blocked = run([(DISPATCHER, BOX_DISPATCHER)])
    assert blocked.blockers
    res = run([(DISPATCHER, BOX_DISPATCHER)], acks={DISPATCHER: sha(BOX_DISPATCHER)})
    assert not res.blockers, res.blockers
    assert res.env == {
        "CAMPAIGN_TRANSFER_DESTINATION_POLICY": "context_first",
        "CAMPAIGN_DEFAULT_TRANSFER_DESTINATION": "+15550004321",
    }


def test_dispatcher_with_unknown_transfer_logic_blocks():
    text = 'NO_CALLER_ID = "NO_CALLER_ID_AVAILABLE_FOR_DESTINATION_STATE"\n'
    res = run([(DISPATCHER, text)])
    assert any("base variants" in b for b in res.blockers)


def test_unknown_mount_blocks_with_actionable_reason():
    res = run([("/app/api/services/telephony/call_transfer_manager.py", "x = 1\n")])
    assert res.results[0].action == "block"
    assert (
        "mount_manifest.json" in res.blockers[0]
        and "cutover_plan.py diff" in res.blockers[0]
    )


# The box-local patches on hopwhistle-prod-ash, reduced to the lines the
# manifest keys on (see /opt/dograh-patches on the box).
BOX_ARI_MANAGER = """
            # HOPWHISTLE_ARI_INBOUND_ANY_CONFIG: every ari config shares the
            # Stasis app, and only one connection gets its events, so the
            # number's own config need not match this connection's.
            if not phone_row:
"""
BOX_ARTIFACTS = """
    # HOPWHISTLE_MIXED_ONLY_V1: keep only the mixed recording. The separate
    # user/bot tracks were 2/3 of recording storage and nothing reads them.
    user_audio_wav = bot_audio_wav = None
"""
BOX_ORCHESTRATOR = """
            if (datetime.now(UTC) - lock_time).total_seconds() < 0.5:
"""
BOX_ARI_CARRIER = """
        # HOPWHISTLE_ARI_CARRIER_V1
        "outbound_carrier": value.get("outbound_carrier") or "fractel",
"""


@pytest.mark.parametrize(
    "target,text",
    [
        ("/app/api/services/telephony/ari_manager.py", BOX_ARI_MANAGER),
        ("/app/api/services/workflow_run_artifacts.py", BOX_ARTIFACTS),
        ("/app/api/services/campaign/campaign_orchestrator.py", BOX_ORCHESTRATOR),
    ],
)
def test_box_local_patches_ported_to_source_are_removed(target, text):
    res = run([(target, text)])
    assert not res.blockers, res.blockers
    assert res.results[0].action == "remove"


@pytest.mark.parametrize(
    "target",
    [
        "/app/api/services/telephony/providers/ari/__init__.py",
        "/app/api/services/telephony/providers/ari/config.py",
    ],
)
def test_box_local_ari_carrier_patch_is_removed(target):
    res = run([(target, BOX_ARI_CARRIER)])
    assert not res.blockers, res.blockers
    assert res.results[0].action == "remove"


# The box's provider.py: the fork's Oct 15 file (ARI_PJSIP_DEFAULT_TRUNK,
# ARI_TRANSFER_DEFAULT_CALLER_ID) plus the caller-trunk kit and three box-only
# additions, reduced to the lines the manifest keys on.
BOX_PROVIDER = """
def _pjsip_endpoint(number: str) -> str:
    trunk = os.environ.get("ARI_PJSIP_DEFAULT_TRUNK", "").strip()
    # HOPWHISTLE_FRACTEL_PREFIX_V1: FracTEL picks the route/account by
    prefix = os.environ.get("DOGRAH_FRACTEL_TECH_PREFIX", "").strip()
# HOPWHISTLE_ARI_CALLER_TRUNK_V1: choose the outbound trunk from the caller ID.
# HOPWHISTLE_PVN_SHAKEN_V1: sign FracTEL-bound calls with PVN's STIR/SHAKEN
        # HOPWHISTLE_ARI_CARRIER_V1
        self.outbound_carrier = config.get("outbound_carrier") or "fractel"
            transfer_caller_id = (
                kwargs.get("caller_id")
                or os.environ.get("ARI_TRANSFER_DEFAULT_CALLER_ID", "").strip()
            )
"""


def test_box_provider_patch_is_removed_with_its_settings_carried_over():
    res = run(
        [(PROVIDER, BOX_PROVIDER)],
        env={
            "ARI_PJSIP_DEFAULT_TRUNK": "fractel",
            "DOGRAH_FRACTEL_TECH_PREFIX": "6125#",
            "DOGRAH_ARI_CALLER_TRUNKS": "anveo/nanp11:+15550001111",
        },
    )
    assert not res.blockers, res.blockers
    assert res.results[0].action == "remove"
    assert res.env["ARI_FRACTEL_TECH_PREFIX"] == "6125#"
    assert res.env["ARI_CALLER_TRUNKS"] == "anveo/nanp11:+15550001111"


def test_pvn_stir_directory_mount_is_kept():
    res = run([("/run/pvn-stir", "")])
    assert not res.blockers, res.blockers
    assert res.results[0].action == "keep"


def test_unrecognised_content_of_known_mount_blocks_until_acknowledged():
    text = "nothing the manifest knows\n"
    target = "/app/api/routes/reports.py"
    assert run([(target, text)]).blockers
    res = run([(target, text)], acks={target: sha(text)})
    assert not res.blockers and res.results[0].action == "remove"
    assert res.warnings


def test_identical_to_target_is_removed():
    target = "/app/api/routes/reports.py"
    res = run([(target, IMAGE[target])])
    assert not res.blockers and res.results[0].action == "remove"


def test_target_image_without_the_behavior_blocks():
    image = dict(IMAGE)
    image["/app/api/services/workflow/pipecat_engine_callbacks.py"] = (
        "# no hotfix here\n"
    )
    target = "/app/api/services/workflow/pipecat_engine_callbacks.py"
    res = run([(target, "# HOPWHISTLE_TRANSFER_DURATION_HOTFIX_V1\n")], image=image)
    assert any(
        "target image lacks transfer_duration_hotfix_callbacks" in b
        for b in res.blockers
    )


def test_unreadable_host_file_blocks():
    mounts = [Volume("", "/opt/missing.py", "/app/api/routes/reports.py", "ro", 0)]
    res = evaluate(MANIFEST, mounts, lambda p: None, IMAGE, {}, {}, {})
    assert any("cannot read" in b for b in res.blockers)


def test_exclusive_variants_block():
    text = V3_PROVIDER + CALLER_TRUNK_PROVIDER
    res = run([(PROVIDER, text)], acks={PROVIDER: sha(text)})
    assert any("cannot both apply" in b for b in res.blockers)


def test_operator_value_resolves_a_failed_derivation():
    text = KIT_DISPATCHER.replace(' or "+15550001234"', "")
    assert run([(DISPATCHER, text)]).blockers
    res = run(
        [(DISPATCHER, text)],
        operator={
            "CAMPAIGN_TRANSFER_DESTINATION_POLICY": "forced_default",
            "CAMPAIGN_DEFAULT_TRANSFER_DESTINATION": "+15550006666",
        },
    )
    assert not res.blockers
    assert res.env_origin["CAMPAIGN_DEFAULT_TRANSFER_DESTINATION"] == "operator"


def test_changing_a_running_value_is_warned():
    text = STOCK_PROVIDER.replace("fractel", "vonage")
    res = run(
        [(PROVIDER, text)],
        acks={PROVIDER: sha(text)},
        env={"ARI_PJSIP_DEFAULT_TRUNK": "fractel"},
    )
    assert not res.blockers
    assert res.env["ARI_PJSIP_DEFAULT_TRUNK"] == "vonage"
    assert any(
        "changes from the running value (fractel -> vonage)" in w for w in res.warnings
    )


def test_patch_marker_with_stock_literal_is_not_a_known_file():
    text = STOCK_PROVIDER + "# HOPWHISTLE_ARI_TRUNK_V1\n"
    res = run([(PROVIDER, text)], acks={PROVIDER: sha(text)})
    assert any("cannot both apply" in b for b in res.blockers)


@pytest.mark.parametrize("target", sorted(MANIFEST["mounts"]))
def test_every_manifest_entry_is_well_formed(target):
    entry = MANIFEST["mounts"][target]
    assert entry["action"] in ("remove_when_preserved", "keep")
    ids = {v["id"] for v in entry["variants"]}
    for group in entry.get("exclusive", []):
        assert set(group) <= ids
    assert set(entry.get("require_one_of", [])) <= ids
