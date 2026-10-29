"""hopwhistle-prod-ash's box-local ARI patch, reproduced in source.

The box bind-mounts a patched ``providers/ari/provider.py``
(/opt/dograh-patches/ari-trunk) that adds three things on top of the fork's
Oct 15 image: the FracTEL tech prefix (HOPWHISTLE_FRACTEL_PREFIX_V1), the Mash
carrier (HOPWHISTLE_ARI_CARRIER_V1) and STIR/SHAKEN signing with PVN's
certificate (HOPWHISTLE_PVN_SHAKEN_V1). The reference functions below restate
the patch's dial-string logic; the ported code must agree with them.
"""

import base64
import json
import os
import re
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.x509.oid import NameOID

from api.services.telephony.providers.ari import stir_shaken
from api.services.telephony.providers.ari.dialing import (
    outbound_endpoint,
    transfer_endpoint,
)
from api.services.telephony.providers.ari.provider import ARIProvider

_ENV = (
    "ARI_PJSIP_DEFAULT_TRUNK",
    "ARI_PJSIP_TRANSFER_TRUNK",
    "ARI_DIAL_ANY_TECHNOLOGY",
    "ARI_DIAL_PREFIX",
    "ARI_DIAL_FORMAT",
    "ARI_CALLER_TRUNKS",
    "ARI_FRACTEL_TECH_PREFIX",
    "ARI_STIR_SHAKEN_DIR",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    stir_shaken._cache.clear()


# ---- reference: the box's patched provider.py -------------------------------


def _ref_pjsip_endpoint(number, env):
    trunk = env.get("ARI_PJSIP_DEFAULT_TRUNK", "").strip()
    prefix = env.get("DOGRAH_FRACTEL_TECH_PREFIX", "").strip()
    if trunk == "fractel" and prefix:
        m = re.fullmatch(r"\+?1?([2-9]\d{9})", re.sub(r"[^\d+]", "", number))
        if m:
            return f"PJSIP/{prefix}1{m.group(1)}@{trunk}"
    if trunk and "@" not in number:
        return f"PJSIP/{number}@{trunk}"
    return f"PJSIP/{number}"


def _ref_caller_route(caller, env):
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
    return routes.get(re.sub(r"\D", "", caller or "")[-10:]) if caller else None


def _ref_format(number, fmt):
    digits = re.sub(r"\D", "", number or "")
    if not re.fullmatch(r"1?\d{10}", digits) or fmt not in ("nanp11", "nanp10"):
        return number
    return digits[-10:] if fmt == "nanp10" else "1" + digits[-10:]


def _ref_outbound(to_number, caller, carrier, env):
    if to_number.startswith(("SIP/", "PJSIP/")):
        return to_number
    route = _ref_caller_route(caller, env)
    if route:
        endpoint = f"PJSIP/{_ref_format(to_number, route[1])}@{route[0]}"
    else:
        endpoint = _ref_pjsip_endpoint(to_number, env)
    if carrier == "mash" and not route:
        endpoint = f"PJSIP/{_ref_format(to_number, 'nanp11')}@mash"
    return endpoint


def _apply(monkeypatch, env):
    """Box environment -> the settings the cutover planner derives."""
    mapping = {
        "ARI_PJSIP_DEFAULT_TRUNK": "ARI_PJSIP_DEFAULT_TRUNK",
        "DOGRAH_FRACTEL_TECH_PREFIX": "ARI_FRACTEL_TECH_PREFIX",
        "DOGRAH_ARI_CALLER_TRUNKS": "ARI_CALLER_TRUNKS",
    }
    for box_name, name in mapping.items():
        if env.get(box_name):
            monkeypatch.setenv(name, env[box_name])


BOX_ENVS = [
    {"ARI_PJSIP_DEFAULT_TRUNK": "fractel"},
    {"ARI_PJSIP_DEFAULT_TRUNK": "fractel", "DOGRAH_FRACTEL_TECH_PREFIX": "6125#"},
    {
        "ARI_PJSIP_DEFAULT_TRUNK": "fractel",
        "DOGRAH_FRACTEL_TECH_PREFIX": "6125#",
        "DOGRAH_ARI_CALLER_TRUNKS": "anveo/nanp11:+15550001111;vonage:5550002222",
    },
    {"ARI_PJSIP_DEFAULT_TRUNK": "anveo", "DOGRAH_FRACTEL_TECH_PREFIX": "6125#"},
]
NUMBERS = ["+14155550123", "14155550123", "4155550123", "(415) 555-0123", "1001"]
CALLERS = [None, "+15550001111", "15550002222", "+15559999999"]


@pytest.mark.parametrize("env", BOX_ENVS)
@pytest.mark.parametrize("carrier", ["fractel", "mash"])
def test_outbound_matches_box_patch(monkeypatch, env, carrier):
    _apply(monkeypatch, env)
    for number in NUMBERS + ["PJSIP/1001@office", "SIP/foo"]:
        for caller in CALLERS:
            assert outbound_endpoint(number, caller, carrier=carrier) == (
                _ref_outbound(number, caller, carrier, env)
            ), (number, caller)


@pytest.mark.parametrize("env", BOX_ENVS)
def test_transfer_matches_box_patch(monkeypatch, env):
    # The box's transfers went through the same _pjsip_endpoint, so the
    # FracTEL prefix applied to them too.
    _apply(monkeypatch, env)
    for number in NUMBERS:
        assert transfer_endpoint(number) == _ref_pjsip_endpoint(number, env), number


def test_fractel_prefix_keeps_hash_and_beats_dial_prefix(monkeypatch):
    monkeypatch.setenv("ARI_PJSIP_DEFAULT_TRUNK", "fractel")
    monkeypatch.setenv("ARI_FRACTEL_TECH_PREFIX", "6125#")
    monkeypatch.setenv("ARI_DIAL_PREFIX", "0123")
    assert outbound_endpoint("+14155550123") == "PJSIP/6125#14155550123@fractel"


def test_provider_reads_carrier_and_pool_numbers():
    base = {"ari_endpoint": "http://a:8088", "app_name": "x", "app_password": "y"}
    assert ARIProvider({**base}).outbound_carrier == "fractel"
    provider = ARIProvider(
        {
            **base,
            "from_numbers": ["+15550000001"],
            "outbound_carrier": "mash",
            "pool_numbers": ["+15550000009"],
        }
    )
    assert provider.outbound_carrier == "mash"
    assert provider.from_numbers == ["+15550000009"]


# ---- STIR/SHAKEN -------------------------------------------------------------


def _write_stir_dir(path, *, enabled=True, dids=("15550001111",), days_valid=30):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=days_valid))
        .sign(key, hashes.SHA256())
    )
    (path / "pvn-private-key.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    (path / "pvn-certificate.pem").write_bytes(
        cert.public_bytes(serialization.Encoding.PEM)
    )
    (path / "pvn-shaken.json").write_text(
        json.dumps({"x5u": "https://cert.example/pvn.pem", "origid": "abc-123"})
    )
    (path / "pvn-dids.txt").write_text("\n".join(dids) + "\n")
    if enabled:
        (path / "ENABLED").write_text("")
    return key


def _unb64(part):
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def test_signs_fractel_call_with_valid_passport(monkeypatch, tmp_path):
    key = _write_stir_dir(tmp_path)
    monkeypatch.setenv("ARI_STIR_SHAKEN_DIR", str(tmp_path))

    variables = stir_shaken.identity_variables(
        "PJSIP/6125#14155550123@fractel", "+15550001111"
    )

    identity = variables["PJSIP_HEADER(add,Identity)"]
    assert "PJSIP_HEADER(add,Date)" in variables
    token, params = identity.split(";", 1)
    assert params == "info=<https://cert.example/pvn.pem>;alg=ES256;ppt=shaken"
    header, payload, signature = token.split(".")
    claims = json.loads(_unb64(payload))
    assert claims["attest"] == "A"
    assert claims["orig"] == {"tn": "15550001111"}
    assert claims["dest"] == {"tn": ["14155550123"]}
    assert claims["origid"] == "abc-123"
    assert json.loads(_unb64(header))["x5u"] == "https://cert.example/pvn.pem"
    raw = _unb64(signature)
    der = encode_dss_signature(
        int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
    )
    key.public_key().verify(
        der, f"{header}.{payload}".encode(), ec.ECDSA(hashes.SHA256())
    )


def test_unknown_caller_gets_attestation_b(monkeypatch, tmp_path):
    _write_stir_dir(tmp_path)
    monkeypatch.setenv("ARI_STIR_SHAKEN_DIR", str(tmp_path))
    variables = stir_shaken.identity_variables(
        "PJSIP/+14155550123@fractel", "+16465550000"
    )
    payload = variables["PJSIP_HEADER(add,Identity)"].split(".")[1]
    assert json.loads(_unb64(payload))["attest"] == "B"


@pytest.mark.parametrize(
    "endpoint,caller,enabled",
    [
        ("PJSIP/14155550123@fractel", "+15550001111", False),  # no ENABLED file
        ("PJSIP/14155550123@mash", "+15550001111", True),  # not FracTEL
        ("PJSIP/14155550123@fractel", None, True),  # no caller ID
        ("PJSIP/1001@fractel", "+15550001111", True),  # not a NANP number
    ],
)
def test_not_signed(monkeypatch, tmp_path, endpoint, caller, enabled):
    _write_stir_dir(tmp_path, enabled=enabled)
    monkeypatch.setenv("ARI_STIR_SHAKEN_DIR", str(tmp_path))
    assert stir_shaken.identity_variables(endpoint, caller) == {}


def test_broken_files_never_block_the_call(monkeypatch, tmp_path):
    _write_stir_dir(tmp_path)
    (tmp_path / "pvn-private-key.pem").write_text("not a key")
    monkeypatch.setenv("ARI_STIR_SHAKEN_DIR", str(tmp_path))
    assert (
        stir_shaken.identity_variables("PJSIP/14155550123@fractel", "+15550001111")
        == {}
    )


def test_missing_directory_is_unsigned(monkeypatch, tmp_path):
    monkeypatch.setenv("ARI_STIR_SHAKEN_DIR", os.fspath(tmp_path / "absent"))
    assert (
        stir_shaken.identity_variables("PJSIP/14155550123@fractel", "+15550001111")
        == {}
    )
