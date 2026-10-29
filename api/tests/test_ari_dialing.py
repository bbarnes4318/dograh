"""ARI dial strings: defaults unchanged, patch-only behaviors reproduced.

The reference functions below restate, line for line, what the hoppwhistle
patch kits inject into provider.py (deploy/dograh/ari-trunk/
apply_ari_trunk_patch.py V1-V3 and apply_caller_trunk_patch.py). The ported
module must produce the same dial strings once the equivalent settings are
configured (see deploy/hopwhistle/cutover/mount_manifest.json).
"""

import os
import re
from types import SimpleNamespace

import pytest

from api.services.campaign.campaign_call_dispatcher import (
    resolve_transfer_destination,
    transfer_destination_policy,
)
from api.services.telephony.providers.ari.dialing import (
    outbound_caller_id,
    outbound_endpoint,
    transfer_endpoint,
)

_ENV = (
    "ARI_PJSIP_DEFAULT_TRUNK",
    "ARI_PJSIP_TRANSFER_TRUNK",
    "ARI_DIAL_ANY_TECHNOLOGY",
    "ARI_DIAL_PREFIX",
    "ARI_DIAL_FORMAT",
    "ARI_CALLER_TRUNKS",
    "CAMPAIGN_DEFAULT_TRANSFER_DESTINATION",
    "CAMPAIGN_TRANSFER_DESTINATION_POLICY",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)


# ---- references: the patched provider.py, per kit version -------------------

_TECH = re.compile(r"^[A-Za-z][A-Za-z0-9]*/")


def _ref_trunk(env, kind="outbound"):
    default = env.get("DOGRAH_ARI_TRUNK", "fractel").strip() or "fractel"
    if kind == "transfer":
        return env.get("DOGRAH_ARI_TRANSFER_TRUNK", default).strip() or default
    return default


def _ref_dial_number(env, number):
    prefix = re.sub(r"\D", "", env.get("DOGRAH_ARI_DIAL_PREFIX", ""))
    if not prefix:
        return number
    digits = re.sub(r"\D", "", number)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return number
    return f"{prefix}1{digits}"


def _ref_outbound_number(env, number):
    if re.sub(r"\D", "", env.get("DOGRAH_ARI_DIAL_PREFIX", "")):
        return _ref_dial_number(env, number)
    fmt = env.get("DOGRAH_ARI_DIAL_FORMAT", "e164").strip().lower()
    if fmt not in ("nanp11", "nanp10"):
        return number
    if not re.fullmatch(r"\+?1?\d{10}", number.strip()):
        return number
    digits = re.sub(r"\D", "", number)[-10:]
    return digits if fmt == "nanp10" else f"1{digits}"


def ref_v3_outbound(env, to_number):
    if _TECH.match(to_number.strip()):
        return to_number
    return f"PJSIP/{_ref_outbound_number(env, to_number)}@{_ref_trunk(env)}"


def ref_v3_transfer(env, destination):
    if _TECH.match(destination.strip()):
        return destination
    return f"PJSIP/{destination}@{_ref_trunk(env, 'transfer')}"


def _ref_fork_endpoint(env, number):
    trunk = env.get("ARI_PJSIP_DEFAULT_TRUNK", "").strip()
    if trunk and "@" not in number:
        return f"PJSIP/{number}@{trunk}"
    return f"PJSIP/{number}"


def _ref_caller_routes(env):
    routes = {}
    for part in env.get("DOGRAH_ARI_CALLER_TRUNKS", "").split(";"):
        head, sep, numbers = part.partition(":")
        trunk, _, fmt = head.strip().partition("/")
        if not sep or not re.fullmatch(r"[A-Za-z0-9_.-]+", trunk or ""):
            continue
        fmt = (fmt or "e164").strip().lower()
        for n in numbers.split(","):
            digits = re.sub(r"\D", "", n)[-10:]
            if len(digits) == 10:
                routes[digits] = (trunk, fmt)
    return routes


def _ref_format(number, fmt):
    digits = re.sub(r"\D", "", number or "")
    if not re.fullmatch(r"1?\d{10}", digits) or fmt not in ("nanp11", "nanp10"):
        return number
    return digits[-10:] if fmt == "nanp10" else "1" + digits[-10:]


def ref_caller_outbound(env, to_number, caller):
    if to_number.startswith(("SIP/", "PJSIP/")):
        return to_number
    route = (
        _ref_caller_routes(env).get(re.sub(r"\D", "", caller or "")[-10:])
        if caller
        else None
    )
    if not route:
        return _ref_fork_endpoint(env, to_number)
    trunk, fmt = route
    return f"PJSIP/{_ref_format(to_number, fmt)}@{trunk}"


def ref_caller_id(env, caller):
    route = (
        _ref_caller_routes(env).get(re.sub(r"\D", "", caller or "")[-10:])
        if caller
        else None
    )
    return _ref_format(caller, route[1]) if route else caller


NUMBERS = [
    "+15551230000",
    "15551230000",
    "5551230000",
    "+445551230000",
    "1001",
    "1001@office",
    "SIP/1001",
    "PJSIP/1001@trunk",
    "Local/5551230000@hopwhistle-transfer/n",
    "IAX2/peer/100",
]


def _apply(monkeypatch, env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)


# ---- defaults ------------------------------------------------------------------


class TestDefaultsUnchanged:
    def test_no_settings_matches_previous_source(self):
        assert outbound_endpoint("+15551230000") == "PJSIP/+15551230000"
        assert outbound_endpoint("SIP/1001") == "SIP/1001"
        # Before the port, only SIP/ and PJSIP/ passed through.
        assert outbound_endpoint("Local/x@ctx/n") == "PJSIP/Local/x@ctx/n"
        assert transfer_endpoint("+15551230000") == "PJSIP/+15551230000"
        assert outbound_caller_id("+15551230000") == "+15551230000"

    def test_default_trunk_used_for_both_legs(self, monkeypatch):
        monkeypatch.setenv("ARI_PJSIP_DEFAULT_TRUNK", "fractel")
        assert outbound_endpoint("+15551230000") == "PJSIP/+15551230000@fractel"
        assert transfer_endpoint("+15551230000") == "PJSIP/+15551230000@fractel"
        assert outbound_endpoint("1001@office") == "PJSIP/1001@office"


# ---- equivalence with the patch kits ------------------------------------------------


V3_CASES = [
    {},
    {"DOGRAH_ARI_TRUNK": "twilio"},
    {"DOGRAH_ARI_TRUNK": "twilio", "DOGRAH_ARI_TRANSFER_TRUNK": "fractel"},
    {"DOGRAH_ARI_TRUNK": "anveo", "DOGRAH_ARI_DIAL_PREFIX": "012345"},
    {"DOGRAH_ARI_TRUNK": "vonage", "DOGRAH_ARI_DIAL_FORMAT": "nanp11"},
    {"DOGRAH_ARI_TRUNK": "vonage", "DOGRAH_ARI_DIAL_FORMAT": "nanp10"},
    {"DOGRAH_ARI_DIAL_FORMAT": "nanp11", "DOGRAH_ARI_DIAL_PREFIX": "99"},
]


def _translate_v3(patch_env):
    """What the cutover planner derives for a V1-V3 patched provider.py."""
    out = {
        "ARI_PJSIP_DEFAULT_TRUNK": patch_env.get("DOGRAH_ARI_TRUNK") or "fractel",
        "ARI_DIAL_ANY_TECHNOLOGY": "true",
    }
    transfer = patch_env.get("DOGRAH_ARI_TRANSFER_TRUNK")
    if transfer and transfer != out["ARI_PJSIP_DEFAULT_TRUNK"]:
        out["ARI_PJSIP_TRANSFER_TRUNK"] = transfer
    if patch_env.get("DOGRAH_ARI_DIAL_PREFIX"):
        out["ARI_DIAL_PREFIX"] = patch_env["DOGRAH_ARI_DIAL_PREFIX"]
    if patch_env.get("DOGRAH_ARI_DIAL_FORMAT"):
        out["ARI_DIAL_FORMAT"] = patch_env["DOGRAH_ARI_DIAL_FORMAT"]
    return out


@pytest.mark.parametrize("patch_env", V3_CASES)
def test_matches_trunk_patch_v3(monkeypatch, patch_env):
    _apply(monkeypatch, _translate_v3(patch_env))
    for number in NUMBERS:
        if "@" in number and not number.startswith(
            ("SIP/", "PJSIP/", "Local/", "IAX2/")
        ):
            continue  # the patch appends a second @trunk; both are unusable
        assert outbound_endpoint(number) == ref_v3_outbound(patch_env, number), number
        assert transfer_endpoint(number) == ref_v3_transfer(patch_env, number), number


CALLER_CASES = [
    (
        {"DOGRAH_ARI_CALLER_TRUNKS": "vonage/nanp11:+15550001111,+15550002222"},
        "+15550001111",
    ),
    ({"DOGRAH_ARI_CALLER_TRUNKS": "vonage/nanp10:5550001111"}, "+15550001111"),
    (
        {
            "DOGRAH_ARI_CALLER_TRUNKS": "vonage:+15550001111",
            "ARI_PJSIP_DEFAULT_TRUNK": "fractel",
        },
        "+15550009999",
    ),
    (
        {
            "DOGRAH_ARI_CALLER_TRUNKS": "bad trunk:+15550001111;twilio/nanp11:+15550001111"
        },
        "+15550001111",
    ),
    (
        {"DOGRAH_ARI_CALLER_TRUNKS": "malformed", "ARI_PJSIP_DEFAULT_TRUNK": "fractel"},
        "+15550001111",
    ),
    ({"DOGRAH_ARI_CALLER_TRUNKS": "vonage/nanp11:+15550001111"}, None),
]


@pytest.mark.parametrize("patch_env,caller", CALLER_CASES)
def test_matches_caller_trunk_patch(monkeypatch, patch_env, caller):
    env = {"ARI_CALLER_TRUNKS": patch_env["DOGRAH_ARI_CALLER_TRUNKS"]}
    if "ARI_PJSIP_DEFAULT_TRUNK" in patch_env:
        env["ARI_PJSIP_DEFAULT_TRUNK"] = patch_env["ARI_PJSIP_DEFAULT_TRUNK"]
    _apply(monkeypatch, env)
    for number in NUMBERS:
        if number.startswith(("Local/", "IAX2/")):
            continue  # the caller-trunk patch never changed technology handling
        assert outbound_endpoint(number, caller) == ref_caller_outbound(
            patch_env, number, caller
        ), number
    assert outbound_caller_id(caller) == ref_caller_id(patch_env, caller)


def test_transfers_never_prefixed_or_reformatted(monkeypatch):
    _apply(
        monkeypatch,
        {
            "ARI_PJSIP_DEFAULT_TRUNK": "vonage",
            "ARI_DIAL_PREFIX": "012345",
            "ARI_DIAL_FORMAT": "nanp11",
            "ARI_CALLER_TRUNKS": "vonage/nanp11:+15550001111",
        },
    )
    assert transfer_endpoint("+15551230000") == "PJSIP/+15551230000@vonage"


def test_any_technology_only_when_enabled(monkeypatch):
    dest = "Local/5551230000@hopwhistle-transfer/n"
    assert transfer_endpoint(dest) == f"PJSIP/{dest}"
    monkeypatch.setenv("ARI_DIAL_ANY_TECHNOLOGY", "true")
    assert transfer_endpoint(dest) == dest


def test_provider_uses_dialing_module():
    # Guard against the provider drifting back to inline endpoint building.
    path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "services",
        "telephony",
        "providers",
        "ari",
        "provider.py",
    )
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    assert re.search(r"outbound_endpoint\(\s*to_number, from_number", source)
    assert "outbound_caller_id(from_number)" in source
    assert "transfer_endpoint(destination)" in source


# ---- transfer destination policy -------------------------------------------------


def _run(dest=None):
    return SimpleNamespace(
        context_variables={"transfer_destination": dest} if dest else {}
    )


def _campaign(dest=None):
    return SimpleNamespace(
        orchestrator_metadata={"transfer_destination": dest} if dest else {}
    )


class TestTransferDestinationPolicy:
    def test_default_is_context_first(self, monkeypatch):
        monkeypatch.setenv("CAMPAIGN_DEFAULT_TRANSFER_DESTINATION", "+15550000003")
        assert transfer_destination_policy() == "context_first"
        assert (
            resolve_transfer_destination(
                _run("+15550000001"), _campaign("+15550000002")
            )
            == "+15550000001"
        )
        assert (
            resolve_transfer_destination(_run(), _campaign("+15550000002"))
            == "+15550000002"
        )
        assert resolve_transfer_destination(_run(), _campaign()) == "+15550000003"

    def test_forced_default_ignores_lead_and_campaign(self, monkeypatch):
        monkeypatch.setenv("CAMPAIGN_TRANSFER_DESTINATION_POLICY", "forced_default")
        monkeypatch.setenv("CAMPAIGN_DEFAULT_TRANSFER_DESTINATION", "+15550000003")
        assert (
            resolve_transfer_destination(
                _run("+15550000001"), _campaign("+15550000002")
            )
            == "+15550000003"
        )

    def test_forced_default_without_destination_fails_closed(self, monkeypatch):
        monkeypatch.setenv("CAMPAIGN_TRANSFER_DESTINATION_POLICY", "forced_default")
        with pytest.raises(
            ValueError, match="requires CAMPAIGN_DEFAULT_TRANSFER_DESTINATION"
        ):
            resolve_transfer_destination(_run("+15550000001"), _campaign())

    def test_unknown_policy_fails_closed(self, monkeypatch):
        monkeypatch.setenv("CAMPAIGN_TRANSFER_DESTINATION_POLICY", "forced")
        with pytest.raises(ValueError, match="is not one of"):
            resolve_transfer_destination(_run("+15550000001"), _campaign())
