"""Vonage telephony provider package."""

from typing import Any, Dict

from api.services.telephony.registry import (
    ProviderSpec,
    ProviderUIField,
    ProviderUIMetadata,
    register,
)

from .config import VonageConfigurationRequest, VonageConfigurationResponse
from .provider import VonageProvider
from .transport import create_transport


def _config_loader(value: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "provider": "vonage",
        "application_id": value.get("application_id"),
        "private_key": value.get("private_key"),
        "api_key": value.get("api_key"),
        "api_secret": value.get("api_secret"),
        "signature_secret": value.get("signature_secret"),
        "amd_enabled": value.get("amd_enabled", False),
        "from_numbers": value.get("from_numbers", []),
    }


_UI_METADATA = ProviderUIMetadata(
    display_name="Vonage",
    docs_url="https://docs.dograh.com/integrations/telephony/vonage",
    fields=[
        ProviderUIField(
            name="application_id",
            label="Application ID",
            type="text",
            description="UUID of the Vonage Voice application (Applications page)",
            placeholder="aaaaaaaa-bbbb-cccc-dddd-0123456789ab",
        ),
        ProviderUIField(
            name="private_key",
            label="Private Key",
            type="textarea",
            sensitive=True,
            description=(
                "Contents of the private.key file downloaded when the application "
                "was created, including the BEGIN/END PRIVATE KEY lines. Used to "
                "sign Voice API requests (outbound calls, hangup, transfers)."
            ),
            placeholder="-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----",
        ),
        ProviderUIField(
            name="api_key",
            label="API Key",
            type="text",
            sensitive=True,
            description=(
                "Account API key (Dashboard → API Settings). Identifies the "
                "account on signed webhooks and routes inbound calls."
            ),
        ),
        ProviderUIField(
            name="api_secret",
            label="API Secret",
            type="password",
            sensitive=True,
            description=(
                "Account API secret. Used only to point the application's "
                "answer URL at Dograh when you assign an inbound workflow."
            ),
        ),
        ProviderUIField(
            name="signature_secret",
            label="Signature Secret",
            type="password",
            sensitive=True,
            description=(
                "Dashboard → API Settings → Signed webhooks (SHA-256/HS256). "
                "Required: Dograh rejects unsigned webhooks and media connections."
            ),
        ),
        ProviderUIField(
            name="from_numbers",
            label="Phone Numbers",
            type="string-array",
            description="E.164 Vonage numbers linked to the application",
        ),
        ProviderUIField(
            name="amd_enabled",
            label="Answering Machine Detection",
            type="boolean",
            required=False,
            description=(
                "Ask Vonage to detect whether outbound calls are answered by a "
                "person or a machine (machine_detection=continue). The result is "
                "stored on the run as answered_by. Vonage may bill this separately."
            ),
        ),
    ],
)


SPEC = ProviderSpec(
    name="vonage",
    provider_cls=VonageProvider,
    config_loader=_config_loader,
    transport_factory=create_transport,
    transport_sample_rate=16000,
    config_request_cls=VonageConfigurationRequest,
    ui_metadata=_UI_METADATA,
    config_response_cls=VonageConfigurationResponse,
    account_id_credential_field="api_key",
)


register(SPEC)


__all__ = [
    "SPEC",
    "VonageConfigurationRequest",
    "VonageConfigurationResponse",
    "VonageProvider",
    "create_transport",
]
