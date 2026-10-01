"""Shared fixtures for Vonage provider tests."""

import hashlib
import time
from types import SimpleNamespace
from typing import Callable, Optional

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

SIGNATURE_SECRET = "vonage-signature-secret-0123456789"
API_KEY = "abcd1234"
APPLICATION_ID = "aaaaaaaa-bbbb-cccc-dddd-0123456789ab"


@pytest.fixture(scope="session")
def rsa_keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
    return private_pem, public_pem


@pytest.fixture
def vonage_config(rsa_keypair) -> Callable[..., dict]:
    def _make(**overrides) -> dict:
        config = {
            "provider": "vonage",
            "api_key": API_KEY,
            "api_secret": "vonage-api-secret",
            "application_id": APPLICATION_ID,
            "private_key": rsa_keypair[0],
            "signature_secret": SIGNATURE_SECRET,
            "from_numbers": ["+15551230002"],
        }
        config.update(overrides)
        return config

    return _make


@pytest.fixture
def vonage_provider(vonage_config):
    from api.services.telephony.providers.vonage.provider import VonageProvider

    def _make(**overrides):
        return VonageProvider(vonage_config(**overrides))

    return _make


@pytest.fixture
def signed_headers() -> Callable[..., dict]:
    """Build an ``Authorization`` header shaped like a Vonage signed callback."""

    def _make(
        body: Optional[str] = "",
        *,
        signature_secret: str = SIGNATURE_SECRET,
        api_key: Optional[str] = API_KEY,
        application_id: Optional[str] = APPLICATION_ID,
        iat: Optional[float] = None,
        exp: Optional[float] = None,
        iss: str = "Vonage",
        include_payload_hash: bool = True,
    ) -> dict:
        claims = {
            "iat": int(time.time() if iat is None else iat),
            "jti": "test-jti",
            "iss": iss,
        }
        if api_key is not None:
            claims["api_key"] = api_key
        if application_id is not None:
            claims["application_id"] = application_id
        if exp is not None:
            claims["exp"] = int(exp)
        if include_payload_hash and body is not None:
            claims["payload_hash"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
        token = jwt.encode(claims, signature_secret, algorithm="HS256")
        return {"authorization": f"Bearer {token}"}

    return _make


@pytest.fixture
def make_workflow_run():
    def _make(
        run_id: int = 123,
        *,
        workflow_id: int = 7,
        organization_id: int = 11,
        telephony_configuration_id: Optional[int] = 5,
        call_id: Optional[str] = "aaaaaaaa-bbbb-cccc-dddd-0123456789ab",
        state: str = "initialized",
        logs: Optional[dict] = None,
        campaign_id: Optional[int] = None,
    ):
        gathered = {"call_id": call_id, "call_uuid": call_id} if call_id else {}
        return SimpleNamespace(
            id=run_id,
            workflow_id=workflow_id,
            workflow=SimpleNamespace(id=workflow_id, organization_id=organization_id),
            initial_context={
                "provider": "vonage",
                "telephony_configuration_id": telephony_configuration_id,
            },
            gathered_context=gathered,
            logs=logs if logs is not None else {},
            state=state,
            campaign_id=campaign_id,
            mode="vonage",
        )

    return _make
