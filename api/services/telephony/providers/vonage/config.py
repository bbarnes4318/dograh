"""Vonage telephony configuration schemas."""

import re
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from .auth import VonagePrivateKeyError, validate_private_key

# Same marker as ``api.services.configuration.masking.MASK_MARKER``; not
# imported to keep this module free of the configuration package's imports.
_MASK_MARKER = "***"

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _is_masked(value: str) -> bool:
    """An unchanged secret re-submitted from the edit form.

    ``preserve_masked_fields`` swaps it back to the stored value after
    validation, so it must not be rejected (or "repaired") here.
    """
    return _MASK_MARKER in value


class VonageConfigurationRequest(BaseModel):
    """Request schema for Vonage configuration.

    Feature → credential map:

    * Outbound calls, hangup, transfers: ``application_id`` + ``private_key``
      (RS256 JWT on the Voice API).
    * Signed answer/event webhooks and the authenticated media WebSocket:
      ``signature_secret`` (HS256) + ``api_key`` (``api_key`` claim, and the
      account id used to route inbound webhooks to this configuration).
    * Programmatic inbound setup (Application API): ``api_key`` +
      ``api_secret``.
    """

    provider: Literal["vonage"] = Field(default="vonage")
    api_key: str = Field(..., min_length=1, description="Vonage API Key")
    api_secret: str = Field(..., min_length=1, description="Vonage API Secret")
    application_id: str = Field(..., min_length=1, description="Vonage Application ID")
    private_key: str = Field(
        ..., min_length=1, description="RSA private key (PEM) for JWT generation"
    )
    signature_secret: str = Field(
        ...,
        min_length=1,
        description="Vonage signature secret used to verify signed webhooks",
    )
    amd_enabled: bool = Field(
        default=False,
        description="Request Vonage answering machine detection on outbound calls",
    )
    from_numbers: List[str] = Field(
        default_factory=list,
        description="List of Vonage phone numbers (E.164)",
    )

    @field_validator("api_key", "api_secret", "signature_secret", "application_id")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("application_id")
    @classmethod
    def _validate_application_id(cls, value: str) -> str:
        if not _UUID_RE.match(value):
            raise ValueError(
                "Application ID must be the UUID shown on the Vonage application "
                "page (e.g. aaaaaaaa-bbbb-cccc-dddd-0123456789ab)"
            )
        return value

    @field_validator("private_key")
    @classmethod
    def _validate_private_key(cls, value: str) -> str:
        if _is_masked(value):
            return value
        try:
            return validate_private_key(value)
        except VonagePrivateKeyError as exc:
            raise ValueError(str(exc)) from None


class VonageConfigurationResponse(BaseModel):
    """Response schema for Vonage configuration with masked sensitive fields."""

    provider: Literal["vonage"] = Field(default="vonage")
    application_id: str  # Not sensitive, can show full
    api_key: str  # Masked
    api_secret: str  # Masked
    private_key: str  # Masked
    signature_secret: Optional[str] = None  # Masked
    amd_enabled: bool = False
    from_numbers: List[str]
