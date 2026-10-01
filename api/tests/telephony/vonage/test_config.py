"""Vonage configuration validation and PEM private-key round trips."""

import jwt
import pytest
from pydantic import ValidationError

from api.schemas.telephony_config import (
    TelephonyConfigurationCreateRequest,
)
from api.services.configuration.masking import is_mask_of, mask_key
from api.services.telephony.providers.vonage.auth import (
    VonagePrivateKeyError,
    generate_api_jwt,
    normalize_private_key,
    validate_private_key,
)
from api.services.telephony.providers.vonage.config import VonageConfigurationRequest

# Mirrors conftest.py (conftest modules are not importable under importlib mode).
SIGNATURE_SECRET = "vonage-signature-secret-0123456789"
API_KEY = "abcd1234"
APPLICATION_ID = "aaaaaaaa-bbbb-cccc-dddd-0123456789ab"


def _payload(private_key: str, **overrides) -> dict:
    data = {
        "provider": "vonage",
        "api_key": API_KEY,
        "api_secret": "secret",
        "application_id": APPLICATION_ID,
        "private_key": private_key,
        "signature_secret": SIGNATURE_SECRET,
    }
    data.update(overrides)
    return data


def test_valid_config(rsa_keypair):
    cfg = VonageConfigurationRequest(**_payload(rsa_keypair[0]))
    assert cfg.private_key == rsa_keypair[0]
    assert cfg.amd_enabled is False


@pytest.mark.parametrize(
    "missing", ["application_id", "private_key", "signature_secret", "api_key"]
)
def test_missing_required_fields_rejected(rsa_keypair, missing):
    data = _payload(rsa_keypair[0])
    data.pop(missing)
    with pytest.raises(ValidationError) as exc:
        VonageConfigurationRequest(**data)
    assert missing in str(exc.value)


def test_invalid_application_id_rejected(rsa_keypair):
    with pytest.raises(ValidationError, match="Application ID must be the UUID"):
        VonageConfigurationRequest(**_payload(rsa_keypair[0], application_id="my-app"))


@pytest.mark.parametrize(
    "bad_key,message",
    [
        ("not a key", "full PEM"),
        (
            "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----",
            "not a valid PEM",
        ),
    ],
)
def test_malformed_private_key_rejected(bad_key, message):
    with pytest.raises(ValidationError, match=message):
        VonageConfigurationRequest(**_payload(bad_key))


def test_non_rsa_private_key_rejected():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    ec_pem = (
        ec.generate_private_key(ec.SECP256R1())
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )
    with pytest.raises(VonagePrivateKeyError, match="RSA"):
        validate_private_key(ec_pem)


@pytest.mark.parametrize(
    "mangle",
    [
        lambda k: k,  # pasted as-is into the textarea
        lambda k: k.replace("\n", "\r\n"),  # Windows clipboard
        lambda k: k.replace("\n", "\\n"),  # copied from a .env / JSON string
        lambda k: f'"{k}"',  # surrounding quotes
        lambda k: k.replace("\n", " "),  # newlines collapsed to spaces
        lambda k: "\n\n  " + k.strip() + "  \n",  # stray whitespace
    ],
)
def test_ui_entered_pem_is_normalized_and_signs(rsa_keypair, mangle):
    private_pem, public_pem = rsa_keypair
    cfg = VonageConfigurationRequest(**_payload(mangle(private_pem)))

    assert cfg.private_key.startswith("-----BEGIN PRIVATE KEY-----\n")
    assert cfg.private_key.endswith("-----END PRIVATE KEY-----\n")
    token = generate_api_jwt(APPLICATION_ID, cfg.private_key)
    claims = jwt.decode(token, public_pem, algorithms=["RS256"])
    assert claims["application_id"] == APPLICATION_ID


def test_masked_private_key_passes_validation_unchanged(rsa_keypair):
    masked = mask_key(rsa_keypair[0])
    cfg = VonageConfigurationRequest(**_payload(masked))
    # Left untouched so preserve_masked_fields can recognise and restore it.
    assert cfg.private_key == masked
    assert is_mask_of(cfg.private_key, rsa_keypair[0])


def test_preserve_masked_fields_restores_real_secrets(rsa_keypair):
    from api.routes.organization import preserve_masked_fields

    existing = _payload(rsa_keypair[0], api_secret="real-api-secret")
    submitted = {
        key: (
            mask_key(value)
            if key in ("private_key", "api_secret", "signature_secret", "api_key")
            else value
        )
        for key, value in existing.items()
    }
    preserve_masked_fields("vonage", submitted, existing)
    assert submitted["private_key"] == rsa_keypair[0]
    assert submitted["api_secret"] == "real-api-secret"
    assert submitted["signature_secret"] == SIGNATURE_SECRET
    assert submitted["api_key"] == API_KEY


def test_legacy_stored_key_with_escaped_newlines_still_signs(rsa_keypair):
    private_pem, public_pem = rsa_keypair
    legacy = private_pem.replace("\n", "\\n")
    token = generate_api_jwt(APPLICATION_ID, legacy)
    jwt.decode(token, public_pem, algorithms=["RS256"])


def test_normalize_private_key_is_idempotent(rsa_keypair):
    once = normalize_private_key(rsa_keypair[0])
    assert normalize_private_key(once) == once


def test_discriminated_union_routes_to_vonage(rsa_keypair):
    req = TelephonyConfigurationCreateRequest(
        name="Vonage prod", config=_payload(rsa_keypair[0], amd_enabled=True)
    )
    assert req.config.provider == "vonage"
    assert req.config.amd_enabled is True


def test_ui_metadata_marks_every_secret_sensitive():
    from api.services.telephony import registry

    fields = {f.name: f for f in registry.get("vonage").ui_metadata.fields}
    for name in ("private_key", "api_key", "api_secret", "signature_secret"):
        assert fields[name].sensitive is True
    assert fields["private_key"].type == "textarea"
    assert fields["signature_secret"].required is True
    assert fields["amd_enabled"].type == "boolean"


# ---------------------------------------------------------------------------
# Full UI → API → storage → reload round trip (DB-backed)
# ---------------------------------------------------------------------------


async def _org_user(db_session, suffix: str):
    user, _ = await db_session.get_or_create_user_by_provider_id(
        f"vonage_cfg_user_{suffix}"
    )
    org, _ = await db_session.get_or_create_organization_by_provider_id(
        f"vonage_cfg_org_{suffix}", user.id
    )
    await db_session.update_user_selected_organization(user.id, org.id)
    return await db_session.get_user_by_id(user.id), org


async def test_pem_round_trip_through_api_and_storage(
    test_client_factory, db_session, rsa_keypair
):
    from api.services.telephony.factory import (
        get_telephony_provider_by_id,
        load_telephony_config_by_id,
    )

    private_pem, public_pem = rsa_keypair
    user, org = await _org_user(db_session, "roundtrip")
    # What a browser textarea submits after a Windows paste.
    ui_pem = private_pem.replace("\n", "\r\n")

    async with test_client_factory(user) as client:
        created = await client.post(
            "/api/v1/organizations/telephony-configs",
            json={
                "name": "Vonage PEM",
                "is_default_outbound": True,
                "config": _payload(ui_pem),
            },
        )
        assert created.status_code == 200, created.text
        body = created.json()
        config_id = body["id"]
        returned_key = body["credentials"]["private_key"]
        # The browser never gets the key or secrets back.
        assert "PRIVATE KEY" not in returned_key
        assert body["credentials"]["signature_secret"] != SIGNATURE_SECRET
        assert body["credentials"]["api_secret"] != "secret"

        # Edit the name only, re-submitting every masked secret unchanged.
        masked = body["credentials"]
        updated = await client.put(
            f"/api/v1/organizations/telephony-configs/{config_id}",
            json={
                "name": "Vonage PEM renamed",
                "config": {**masked, "provider": "vonage", "amd_enabled": True},
            },
        )
        assert updated.status_code == 200, updated.text

    stored = await load_telephony_config_by_id(config_id, org.id)
    assert stored["private_key"] == private_pem  # LF-normalized, not the mask
    assert stored["signature_secret"] == SIGNATURE_SECRET
    assert stored["api_secret"] == "secret"
    assert stored["amd_enabled"] is True

    provider = await get_telephony_provider_by_id(config_id, org.id)
    claims = jwt.decode(provider._generate_jwt(), public_pem, algorithms=["RS256"])
    assert claims["application_id"] == APPLICATION_ID


async def test_api_rejects_malformed_private_key_with_field_name(
    test_client_factory, db_session
):
    user, _ = await _org_user(db_session, "badkey")
    async with test_client_factory(user) as client:
        resp = await client.post(
            "/api/v1/organizations/telephony-configs",
            json={"name": "Bad", "config": _payload("garbage")},
        )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert any(item["loc"][-1] == "private_key" for item in detail)


async def test_multiple_vonage_configs_per_org_stay_isolated(
    test_client_factory, db_session, rsa_keypair
):
    from api.services.telephony.factory import load_telephony_config_by_id

    user, org = await _org_user(db_session, "multi")
    other_user, other_org = await _org_user(db_session, "multi_other")
    ids = []
    async with test_client_factory(user) as client:
        for idx in range(2):
            resp = await client.post(
                "/api/v1/organizations/telephony-configs",
                json={
                    "name": f"Vonage {idx}",
                    "config": _payload(rsa_keypair[0], api_key=f"key{idx}"),
                },
            )
            assert resp.status_code == 200, resp.text
            ids.append(resp.json()["id"])

    first = await load_telephony_config_by_id(ids[0], org.id)
    second = await load_telephony_config_by_id(ids[1], org.id)
    assert (first["api_key"], second["api_key"]) == ("key0", "key1")

    # Another org can never load these configurations.
    with pytest.raises(ValueError):
        await load_telephony_config_by_id(ids[0], other_org.id)
