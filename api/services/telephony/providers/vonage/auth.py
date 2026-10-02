"""Vonage credential handling: private keys, API JWTs, signed-callback JWTs.

Three distinct mechanisms live here:

* **Voice API auth (outbound requests)** — RS256 JWT signed with the
  application's RSA private key, sent as ``Authorization: Bearer``.
* **Signed callbacks (inbound requests)** — Vonage signs every answer/event
  webhook *and* (with ``authorization: {"type": "vonage"}`` on the NCCO
  websocket endpoint) the media WebSocket handshake with an HS256 JWT keyed by
  the account signature secret.
* **Per-run media token** — an HMAC Dograh puts in the NCCO websocket
  ``headers``. Vonage echoes it back in the first ``websocket:connected``
  message, binding the socket to one org/workflow/run/config.

Nothing in this module logs a key, a secret or a full token.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import textwrap
import time
import uuid
from typing import Any, Dict, Mapping, Optional

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from loguru import logger

from .errors import VonageAPIError, VonageErrorCategory

API_JWT_TTL_SECONDS = 300
# Vonage mints a fresh JWT per callback. Anything older than this is treated
# as a replay. Leeway absorbs clock skew between Vonage and this host.
SIGNED_CALLBACK_MAX_AGE_SECONDS = 300
SIGNED_CALLBACK_LEEWAY_SECONDS = 60

WS_TOKEN_HEADER = "dograh_ws_token"
_WS_TOKEN_VERSION = "v1"

_PEM_RE = re.compile(
    r"-----BEGIN (?P<label>[A-Z0-9 ]+)-----(?P<body>.*?)-----END (?P=label)-----",
    re.DOTALL,
)


class VonagePrivateKeyError(ValueError):
    """The supplied private key is not a usable PEM RSA private key."""


def normalize_private_key(raw: Optional[str]) -> str:
    """Return a canonical PEM string for ``raw``.

    Repairs the ways a PEM gets mangled on its way through forms, env files
    and JSON: surrounding quotes, literal ``\\n`` escapes, CRLF line endings,
    and newlines collapsed into spaces. The base64 body is re-wrapped at 64
    columns. Does not validate the key material — see
    :func:`validate_private_key`.
    """
    if raw is None:
        return ""
    key = str(raw).strip()
    if len(key) >= 2 and key[0] == key[-1] and key[0] in ("'", '"'):
        key = key[1:-1].strip()
    key = key.replace("\\r\\n", "\n").replace("\\n", "\n")
    key = key.replace("\r\n", "\n").replace("\r", "\n")

    match = _PEM_RE.search(key)
    if not match:
        return key
    label = match.group("label").strip()
    body = re.sub(r"\s+", "", match.group("body"))
    wrapped = "\n".join(textwrap.wrap(body, 64))
    return f"-----BEGIN {label}-----\n{wrapped}\n-----END {label}-----\n"


def validate_private_key(raw: Optional[str]) -> str:
    """Normalize and validate a Vonage RSA private key; return the PEM.

    Raises :class:`VonagePrivateKeyError` with a user-facing explanation.
    """
    key = normalize_private_key(raw)
    if not key:
        raise VonagePrivateKeyError("Private key is required")
    if "-----BEGIN" not in key or "PRIVATE KEY-----" not in key:
        raise VonagePrivateKeyError(
            "Private key must be the full PEM file Vonage generated, starting "
            "with '-----BEGIN PRIVATE KEY-----'"
        )
    try:
        loaded = serialization.load_pem_private_key(key.encode("utf-8"), None)
    except TypeError as exc:
        raise VonagePrivateKeyError(
            "Encrypted (password-protected) private keys are not supported"
        ) from exc
    except Exception as exc:
        raise VonagePrivateKeyError(
            "Private key is not a valid PEM private key; paste the complete "
            "key including the BEGIN/END lines"
        ) from exc
    if not isinstance(loaded, rsa.RSAPrivateKey):
        raise VonagePrivateKeyError("Vonage requires an RSA private key")
    return key


def generate_api_jwt(
    application_id: Optional[str],
    private_key: Optional[str],
    *,
    ttl_seconds: int = API_JWT_TTL_SECONDS,
) -> str:
    """RS256 JWT for the Vonage Voice API (``Authorization: Bearer``)."""
    if not application_id or not private_key:
        raise VonageAPIError(
            VonageErrorCategory.NOT_CONFIGURED,
            provider_detail="application_id and private_key are required",
            operation="authentication",
        )
    now = int(time.time())
    claims = {
        "application_id": application_id,
        "iat": now,
        "nbf": now - 5,
        "exp": now + ttl_seconds,
        "jti": str(uuid.uuid4()),
    }
    try:
        return jwt.encode(claims, normalize_private_key(private_key), algorithm="RS256")
    except Exception:
        # Never chain the original exception text: some crypto errors echo
        # fragments of the input.
        raise VonageAPIError(
            VonageErrorCategory.MALFORMED_PRIVATE_KEY, operation="authentication"
        ) from None


def header(headers: Mapping[str, str], name: str) -> Optional[str]:
    """Case-insensitive header lookup over a plain mapping."""
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def bearer_token(headers: Mapping[str, str]) -> Optional[str]:
    auth_header = header(headers, "authorization")
    if not auth_header:
        return None
    parts = auth_header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def decode_unverified_claims(headers: Mapping[str, str]) -> Dict[str, Any]:
    """Claims of the bearer JWT *without* verification.

    Only used to route a webhook to a candidate configuration (``api_key``);
    the request is always verified against that configuration afterwards.
    """
    token = bearer_token(headers)
    if not token:
        return {}
    try:
        claims = jwt.decode(
            token,
            options={
                "verify_signature": False,
                "verify_aud": False,
                "verify_exp": False,
            },
        )
    except jwt.InvalidTokenError:
        return {}
    return claims if isinstance(claims, dict) else {}


def verify_signed_jwt(
    headers: Mapping[str, str],
    *,
    signature_secret: Optional[str],
    api_key: Optional[str],
    application_id: Optional[str],
    body: Optional[str] = "",
    check_payload_hash: bool = True,
    now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Verify a Vonage-signed HS256 JWT; return its claims or ``None``.

    Fails closed: a missing token, missing secret, bad signature, wrong
    issuer, mismatched ``api_key``/``application_id``, stale ``iat`` or a
    payload-hash mismatch all return ``None``.
    """
    token = bearer_token(headers)
    if not token:
        logger.warning("Vonage signed request rejected: missing bearer token")
        return None
    if not signature_secret:
        logger.error(
            "Vonage signed request rejected: configuration has no signature_secret"
        )
        return None

    try:
        claims = jwt.decode(
            token,
            signature_secret,
            algorithms=["HS256"],
            leeway=SIGNED_CALLBACK_LEEWAY_SECONDS,
            options={"verify_signature": True, "verify_aud": False},
        )
    except jwt.InvalidTokenError as exc:
        logger.warning(f"Vonage signed request rejected: {type(exc).__name__}")
        return None

    if claims.get("iss") != "Vonage":
        logger.warning("Vonage signed request rejected: unexpected issuer")
        return None

    if not api_key or claims.get("api_key") != api_key:
        logger.warning("Vonage signed request rejected: api_key mismatch")
        return None

    claim_application_id = claims.get("application_id")
    if (
        claim_application_id
        and application_id
        and (claim_application_id != application_id)
    ):
        logger.warning("Vonage signed request rejected: application_id mismatch")
        return None

    current = time.time() if now is None else now
    iat = claims.get("iat")
    if not isinstance(iat, (int, float)):
        logger.warning("Vonage signed request rejected: missing iat")
        return None
    if iat > current + SIGNED_CALLBACK_LEEWAY_SECONDS:
        logger.warning("Vonage signed request rejected: iat in the future")
        return None
    if current - iat > SIGNED_CALLBACK_MAX_AGE_SECONDS + SIGNED_CALLBACK_LEEWAY_SECONDS:
        logger.warning("Vonage signed request rejected: token too old (replay)")
        return None

    payload_hash = claims.get("payload_hash")
    if check_payload_hash and payload_hash:
        actual_hash = hashlib.sha256((body or "").encode("utf-8")).hexdigest()
        if not hmac.compare_digest(actual_hash, str(payload_hash)):
            logger.warning("Vonage signed request rejected: payload hash mismatch")
            return None

    return claims


def _ws_token_message(
    organization_id: int,
    workflow_id: int,
    workflow_run_id: int,
    telephony_configuration_id: Optional[int],
) -> bytes:
    return (
        f"dograh-vonage-ws:{_WS_TOKEN_VERSION}:{int(organization_id)}:"
        f"{int(workflow_id)}:{int(workflow_run_id)}:"
        f"{telephony_configuration_id if telephony_configuration_id is not None else '-'}"
    ).encode("utf-8")


def make_ws_token(
    signature_secret: str,
    *,
    organization_id: int,
    workflow_id: int,
    workflow_run_id: int,
    telephony_configuration_id: Optional[int],
) -> str:
    """Per-run media-socket token embedded in the NCCO websocket headers."""
    if not signature_secret:
        raise VonageAPIError(
            VonageErrorCategory.NOT_CONFIGURED,
            provider_detail="signature_secret is required for media authentication",
            operation="ncco",
        )
    digest = hmac.new(
        signature_secret.encode("utf-8"),
        _ws_token_message(
            organization_id, workflow_id, workflow_run_id, telephony_configuration_id
        ),
        hashlib.sha256,
    ).hexdigest()
    return f"{_WS_TOKEN_VERSION}.{digest}"


def verify_ws_token(
    token: Optional[str],
    signature_secret: Optional[str],
    *,
    organization_id: int,
    workflow_id: int,
    workflow_run_id: int,
    telephony_configuration_id: Optional[int],
) -> bool:
    if not token or not signature_secret:
        return False
    expected = make_ws_token(
        signature_secret,
        organization_id=organization_id,
        workflow_id=workflow_id,
        workflow_run_id=workflow_run_id,
        telephony_configuration_id=telephony_configuration_id,
    )
    return hmac.compare_digest(str(token), expected)
