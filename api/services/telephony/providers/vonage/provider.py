"""
Vonage (Nexmo) implementation of the TelephonyProvider interface.

Call flow (outbound)::

    Dograh --POST /v1/calls (RS256 JWT)--> Vonage --PSTN--> callee
    Vonage --GET answer_url (signed)--> /api/v1/telephony/ncco
    Dograh --NCCO connect/websocket--> Vonage
    Vonage --WSS (signed handshake + per-run token)--> /api/v1/telephony/ws/...
    Vonage --POST event_url (signed)--> /api/v1/telephony/vonage/events/{run}

Media is bidirectional raw 16-bit little-endian linear PCM at 16 kHz
(``audio/l16;rate=16000``), sent in 20 ms (640 byte) binary frames.
"""

import asyncio
import json
import random
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional

import aiohttp
from fastapi import Response
from loguru import logger

from api.enums import TelephonyCallStatus, WorkflowRunMode
from api.services.telephony.base import (
    AnsweringMachineDetectionResult,
    CallInitiationResult,
    NormalizedInboundData,
    ProviderSyncResult,
    TelephonyProvider,
)
from api.utils.common import get_backend_endpoints

from .auth import (
    WS_TOKEN_HEADER,
    bearer_token,
    decode_unverified_claims,
    generate_api_jwt,
    make_ws_token,
    verify_signed_jwt,
    verify_ws_token,
)
from .client import REQUEST_TIMEOUT, VONAGE_API_BASE_URL, VonageVoiceClient
from .errors import VonageAPIError, VonageErrorCategory
from .numbers import (
    VonagePhoneNumberError,
    from_vonage_number,
    to_e164,
    to_vonage_number,
)

if TYPE_CHECKING:
    from fastapi import WebSocket

AUDIO_CONTENT_TYPE = "audio/l16;rate=16000"
# How long an authenticated socket may take to send ``websocket:connected``.
WS_CONNECTED_TIMEOUT_SECONDS = 10.0
# Vonage accepts a ringing_timer of 1-120 seconds.
_MIN_RINGING_TIMER = 1
_MAX_RINGING_TIMER = 120

# Vonage lifecycle states -> Dograh lifecycle. ``rejected``/``failed`` are
# refined by the event ``detail`` in ``normalize_vonage_status``.
VONAGE_STATUS_MAP: Dict[str, TelephonyCallStatus] = {
    "started": TelephonyCallStatus.INITIATED,
    "ringing": TelephonyCallStatus.RINGING,
    "answered": TelephonyCallStatus.ANSWERED,
    "complete": TelephonyCallStatus.COMPLETED,
    "completed": TelephonyCallStatus.COMPLETED,
    "disconnected": TelephonyCallStatus.COMPLETED,
    "busy": TelephonyCallStatus.BUSY,
    "timeout": TelephonyCallStatus.NO_ANSWER,
    "unanswered": TelephonyCallStatus.NO_ANSWER,
    "cancelled": TelephonyCallStatus.CANCELED,
    "rejected": TelephonyCallStatus.FAILED,
    "failed": TelephonyCallStatus.FAILED,
}
# Event ``detail`` values that mean "the callee didn't pick up" rather than a
# hard failure, so campaign retry semantics stay right.
_NO_ANSWER_DETAILS = frozenset({"unavailable", "ring_timeout", "carrier_timeout"})
_DECLINED_DETAILS = frozenset({"declined", "busy"})
# Vonage events that are not call lifecycle transitions.
NON_LIFECYCLE_STATUSES = frozenset({"human", "machine", "transfer", "input", "record"})


def normalize_vonage_status(
    status: Optional[str], detail: Optional[str] = None
) -> Optional[TelephonyCallStatus]:
    """Map a Vonage event ``status`` (+ ``detail``) to Dograh's lifecycle."""
    if not status:
        return None
    status = str(status).lower()
    normalized = VONAGE_STATUS_MAP.get(status)
    if normalized in (TelephonyCallStatus.FAILED,) and detail:
        detail = str(detail).lower()
        if detail in _NO_ANSWER_DETAILS:
            return TelephonyCallStatus.NO_ANSWER
        if detail in _DECLINED_DETAILS:
            return TelephonyCallStatus.BUSY
    return normalized


def _log_ctx(**fields: Any) -> str:
    parts = ["provider=vonage"]
    parts.extend(f"{k}={v}" for k, v in fields.items() if v is not None)
    return " ".join(parts)


class VonageProvider(TelephonyProvider):
    """
    Vonage implementation of TelephonyProvider.
    Uses JWT authentication and NCCO for call control.
    """

    PROVIDER_NAME = WorkflowRunMode.VONAGE.value
    WEBHOOK_ENDPOINT = "ncco"

    # Request kwargs the shared call sites pass for providers that build the
    # media URL at dial time. They are Dograh routing metadata, never part of
    # the Vonage request body.
    _ROUTING_KWARGS = frozenset({"workflow_id", "organization_id", "campaign_id"})

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize VonageProvider with configuration.

        Args:
            config: Dictionary containing:
                - api_key: Vonage API Key
                - api_secret: Vonage API Secret
                - application_id: Vonage Application ID
                - private_key: Private key for JWT generation
                - signature_secret: Signature secret for signed webhooks
                - amd_enabled: Request answering machine detection
                - from_numbers: List of phone numbers to use
        """
        self.api_key = config.get("api_key")
        self.api_secret = config.get("api_secret")
        self.application_id = config.get("application_id")
        self.private_key = config.get("private_key")
        self.signature_secret = config.get("signature_secret")
        self.amd_enabled: bool = bool(config.get("amd_enabled", False))
        self.from_numbers = config.get("from_numbers", [])

        # Handle both single number (string) and multiple numbers (list)
        if isinstance(self.from_numbers, str):
            self.from_numbers = [self.from_numbers]

        self.base_url = VONAGE_API_BASE_URL
        # First ``websocket:connected`` message, consumed during
        # ``authenticate_websocket`` and handed to ``handle_websocket``.
        self._ws_connected_message: Optional[Dict[str, Any]] = None

    @property
    def client(self) -> VonageVoiceClient:
        return VonageVoiceClient(
            self.application_id, self.private_key, base_url=self.base_url
        )

    def _generate_jwt(self) -> str:
        """Generate JWT token for Vonage API authentication."""
        return generate_api_jwt(self.application_id, self.private_key)

    def _select_caller_number(self, from_number: Optional[str]) -> str:
        if from_number is None:
            if not self.from_numbers:
                raise VonageAPIError(
                    VonageErrorCategory.NOT_CONFIGURED,
                    provider_detail="no Vonage phone numbers are configured",
                    operation="call creation",
                )
            from_number = random.choice(self.from_numbers)
        return from_number

    # ======== OUTBOUND ========

    async def initiate_call(
        self,
        to_number: str,
        webhook_url: str,
        workflow_run_id: Optional[int] = None,
        from_number: Optional[str] = None,
        **kwargs: Any,
    ) -> CallInitiationResult:
        """
        Initiate an outbound call via Vonage Voice API.
        """
        if not self.validate_config():
            raise VonageAPIError(
                VonageErrorCategory.NOT_CONFIGURED,
                provider_detail=(
                    "Application ID, private key and at least one phone number "
                    "are required"
                ),
                operation="call creation",
            )

        routing = {k: kwargs.pop(k) for k in list(kwargs) if k in self._ROUTING_KWARGS}

        caller = self._select_caller_number(from_number)
        try:
            vonage_to = to_vonage_number(to_number, field="destination number")
            vonage_from = to_vonage_number(caller, field="caller ID")
        except VonagePhoneNumberError as exc:
            raise VonageAPIError(
                VonageErrorCategory.INVALID_NUMBER,
                provider_detail=str(exc),
                operation="call creation",
            ) from None

        data: Dict[str, Any] = {
            "to": [{"type": "phone", "number": vonage_to}],
            "from": {"type": "phone", "number": vonage_from},
            "answer_url": [webhook_url],
            "answer_method": "GET",
        }

        if workflow_run_id:
            backend_endpoint, _ = await get_backend_endpoints()
            event_url = (
                f"{backend_endpoint}/api/v1/telephony/vonage/events/{workflow_run_id}"
            )
            data.update({"event_url": [event_url], "event_method": "POST"})

        data = self.apply_answering_machine_detection_call_params(data)
        # Remaining kwargs are explicit Vonage request overrides.
        data.update(kwargs)

        ctx = _log_ctx(
            org=routing.get("organization_id"),
            workflow=routing.get("workflow_id"),
            run=workflow_run_id,
            campaign=routing.get("campaign_id"),
            direction="outbound",
        )
        logger.info(f"{ctx} creating call from={vonage_from} to=***{vonage_to[-4:]}")
        try:
            response_data = await self.client.create_call(data)
        except VonageAPIError as exc:
            logger.error(
                f"{ctx} call creation failed category={exc.category.value} "
                f"vonage_status={exc.vonage_status}"
            )
            raise

        call_uuid = response_data["uuid"]
        logger.info(
            f"{ctx} call_uuid={call_uuid} state={response_data.get('status', 'started')}"
        )
        return CallInitiationResult(
            call_id=call_uuid,
            status=response_data.get("status", "started"),
            caller_number=to_e164(caller, field="caller ID"),
            provider_metadata={
                "call_id": call_uuid,
                "call_uuid": call_uuid,
                "conversation_uuid": response_data.get("conversation_uuid"),
            },
            raw_response=response_data,
        )

    async def get_call_status(self, call_id: str) -> Dict[str, Any]:
        """
        Get the current status of a Vonage call.
        """
        if not (self.application_id and self.private_key):
            raise ValueError("Vonage provider not properly configured")
        return await self.client.get_call(call_id)

    async def get_available_phone_numbers(self) -> List[str]:
        """
        Get list of available Vonage phone numbers.
        """
        return self.from_numbers

    def validate_config(self) -> bool:
        """
        Validate Vonage configuration.
        """
        return bool(self.application_id and self.private_key and self.from_numbers)

    async def verify_webhook_signature(
        self, url: str, params: Dict[str, Any], signature: str
    ) -> bool:
        """
        Verify Vonage webhook signature for security.
        Vonage uses JWT for webhook signatures.
        """
        claims = verify_signed_jwt(
            {"authorization": f"Bearer {signature}"},
            signature_secret=self.signature_secret,
            api_key=self.api_key,
            application_id=self.application_id,
            check_payload_hash=False,
        )
        return claims is not None

    # ======== NCCO / MEDIA ========

    def build_websocket_endpoint(
        self,
        *,
        websocket_url: str,
        organization_id: int,
        workflow_id: int,
        workflow_run_id: int,
        telephony_configuration_id: Optional[int],
        call_uuid: Optional[str] = None,
    ) -> Dict[str, Any]:
        """NCCO websocket endpoint with Vonage-signed handshake + run token.

        ``authorization: {"type": "vonage"}`` makes Vonage send
        ``Authorization: Bearer <JWT signed with the signature secret>`` on the
        WebSocket upgrade. The ``headers`` are echoed back in the first
        ``websocket:connected`` message, carrying the per-run token.
        """
        headers = {
            WS_TOKEN_HEADER: make_ws_token(
                self.signature_secret,
                organization_id=organization_id,
                workflow_id=workflow_id,
                workflow_run_id=workflow_run_id,
                telephony_configuration_id=telephony_configuration_id,
            ),
            "workflow_run_id": str(workflow_run_id),
        }
        if call_uuid:
            headers["call_uuid"] = call_uuid
        return {
            "type": "websocket",
            "uri": websocket_url,
            "content-type": AUDIO_CONTENT_TYPE,
            "headers": headers,
            "authorization": {"type": "vonage"},
        }

    async def get_webhook_response(
        self,
        workflow_id: int,
        organization_id: int,
        workflow_run_id: int,
        *,
        telephony_configuration_id: Optional[int] = None,
        call_uuid: Optional[str] = None,
    ) -> str:
        """
        Generate NCCO response for starting a call session.
        NCCO (Nexmo Call Control Objects) is JSON-based, unlike TwiML which is XML.
        """
        _, wss_backend_endpoint = await get_backend_endpoints()
        if telephony_configuration_id is None:
            from api.db import db_client

            run = await db_client.get_workflow_run(
                workflow_run_id, organization_id=organization_id
            )
            telephony_configuration_id = (
                (run.initial_context or {}).get("telephony_configuration_id")
                if run
                else None
            )

        ncco = [
            {
                "action": "connect",
                "endpoint": [
                    self.build_websocket_endpoint(
                        websocket_url=(
                            f"{wss_backend_endpoint}/api/v1/telephony/ws/"
                            f"{workflow_id}/{organization_id}/{workflow_run_id}"
                        ),
                        organization_id=organization_id,
                        workflow_id=workflow_id,
                        workflow_run_id=workflow_run_id,
                        telephony_configuration_id=telephony_configuration_id,
                        call_uuid=call_uuid,
                    )
                ],
            }
        ]

        return json.dumps(ncco)

    def _get_auth_headers(self) -> Dict[str, str]:
        """Generate authorization headers for Vonage API."""
        token = self._generate_jwt()
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async def get_call_cost(self, call_id: str) -> Dict[str, Any]:
        """
        Get cost information for a completed Vonage call.

        Vonage reports ``price`` in the account's billing currency (which is
        not necessarily USD) and does not return the currency on the call
        object. ``cost_usd`` therefore assumes a USD-billed account.

        Args:
            call_id: The Vonage Call UUID

        Returns:
            Dict containing cost information
        """
        try:
            call_data = await self.client.get_call(call_id)
        except Exception as e:
            logger.error(f"{_log_ctx(call_uuid=call_id)} cost lookup failed: {e}")
            return {"cost_usd": 0.0, "duration": 0, "status": "error", "error": str(e)}

        try:
            price = float(call_data.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        try:
            duration = int(float(call_data.get("duration") or 0))
        except (TypeError, ValueError):
            duration = 0

        return {
            "cost_usd": price,
            "duration": duration,
            "status": call_data.get("status", "unknown"),
            "price_unit": "USD",
            "rate": call_data.get("rate", 0),
            "raw_response": call_data,
        }

    def parse_status_callback(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parse Vonage event callback data into generic format.

        ``status`` is ``None`` for events that are not lifecycle transitions
        (AMD ``human``/``machine``, ``transfer``, ``input``...).
        """
        raw_status = data.get("status")
        normalized = normalize_vonage_status(raw_status, data.get("detail"))
        if (
            normalized is None
            and raw_status
            and (str(raw_status).lower() not in NON_LIFECYCLE_STATUSES)
        ):
            # Unknown state: pass the raw value through so it's logged as
            # unexpected rather than silently mapped.
            status: Any = raw_status
        else:
            status = normalized

        duration = data.get("duration")
        return {
            "call_id": data.get("uuid", ""),
            "status": status,
            "from_number": from_vonage_number(data.get("from")) or None,
            "to_number": from_vonage_number(data.get("to")) or None,
            "direction": data.get("direction"),
            "duration": str(duration) if duration is not None else None,
            "extra": data,  # Include all original data
        }

    # ======== ANSWERING MACHINE DETECTION ========

    def supports_answering_machine_detection(self) -> bool:
        """Vonage supports AMD via ``machine_detection`` on ``POST /v1/calls``."""
        return True

    def apply_answering_machine_detection_call_params(
        self, data: Dict[str, Any]
    ) -> Dict[str, Any]:
        if self.amd_enabled:
            # "continue": keep the call up and report human/machine on the
            # event webhook; Dograh's own call hygiene decides what to do.
            data["machine_detection"] = "continue"
        return data

    def parse_answering_machine_detection_result(
        self, data: Dict[str, Any]
    ) -> Optional[AnsweringMachineDetectionResult]:
        status = str(data.get("status") or "").lower()
        if status not in ("human", "machine"):
            return None
        sub_state = data.get("sub_state")
        answered_by = status if not sub_state else f"{status}_{sub_state}"
        return AnsweringMachineDetectionResult(
            call_id=data.get("uuid", ""),
            answered_by=answered_by,
            raw_data=data,
        )

    # ======== WEBSOCKET ========

    async def authenticate_websocket(
        self,
        websocket: "WebSocket",
        *,
        workflow_run: Any,
        workflow_id: int,
        organization_id: int,
    ) -> bool:
        """Authenticate a media WebSocket before the run is marked running.

        1. The upgrade request must carry a Vonage-signed JWT (signature
           secret of *this run's* configuration, matching api_key and
           application_id).
        2. The first message must be ``websocket:connected`` carrying the
           per-run HMAC token bound to org/workflow/run/configuration.
        """
        telephony_configuration_id = (workflow_run.initial_context or {}).get(
            "telephony_configuration_id"
        )
        ctx = _log_ctx(
            org=organization_id,
            workflow=workflow_id,
            run=workflow_run.id,
            cfg=telephony_configuration_id,
        )

        claims = verify_signed_jwt(
            dict(websocket.headers),
            signature_secret=self.signature_secret,
            api_key=self.api_key,
            application_id=self.application_id,
            check_payload_hash=False,
        )
        if claims is None:
            logger.warning(
                f"{ctx} media websocket rejected: handshake not signed by Vonage"
            )
            return False

        try:
            first_msg = await asyncio.wait_for(
                websocket.receive(), timeout=WS_CONNECTED_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            logger.warning(f"{ctx} media websocket rejected: no websocket:connected")
            return False

        text = first_msg.get("text")
        if text is None:
            logger.warning(
                f"{ctx} media websocket rejected: first frame was not "
                "websocket:connected"
            )
            return False
        try:
            message = json.loads(text)
        except (TypeError, ValueError):
            logger.warning(f"{ctx} media websocket rejected: malformed first event")
            return False
        if (
            not isinstance(message, dict)
            or message.get("event") != "websocket:connected"
        ):
            logger.warning(f"{ctx} media websocket rejected: unexpected first event")
            return False

        nested = (
            message.get("headers") if isinstance(message.get("headers"), dict) else {}
        )
        token = message.get(WS_TOKEN_HEADER) or nested.get(WS_TOKEN_HEADER)
        if not verify_ws_token(
            token,
            self.signature_secret,
            organization_id=organization_id,
            workflow_id=workflow_id,
            workflow_run_id=workflow_run.id,
            telephony_configuration_id=telephony_configuration_id,
        ):
            logger.warning(f"{ctx} media websocket rejected: run token mismatch")
            return False

        content_type = str(message.get("content-type") or "")
        if content_type and content_type.replace(" ", "").lower() != AUDIO_CONTENT_TYPE:
            logger.warning(
                f"{ctx} unexpected media content-type {content_type!r}; "
                f"expected {AUDIO_CONTENT_TYPE}"
            )

        self._ws_connected_message = message
        logger.info(f"{ctx} media websocket authenticated")
        return True

    async def handle_websocket(
        self,
        websocket: "WebSocket",
        workflow_id: int,
        organization_id: int,
        workflow_run_id: int,
    ) -> None:
        """
        Handle Vonage-specific WebSocket connection.

        The socket has already been authenticated by ``authenticate_websocket``
        (which consumed the ``websocket:connected`` message).
        """
        from api.db import db_client
        from api.services.pipecat.run_pipeline import run_pipeline_telephony

        if self._ws_connected_message is None:
            # Defensive: never run a pipeline on an unauthenticated socket.
            logger.error(
                f"{_log_ctx(org=organization_id, run=workflow_run_id)} "
                "handle_websocket called without authentication"
            )
            await websocket.close(code=4401, reason="Unauthorized")
            return

        workflow_run = await db_client.get_workflow_run(
            workflow_run_id, organization_id=organization_id
        )
        if not workflow_run:
            await websocket.close(code=4404, reason="Workflow run not found")
            return

        gathered = workflow_run.gathered_context or {}
        message = self._ws_connected_message
        nested = (
            message.get("headers") if isinstance(message.get("headers"), dict) else {}
        )
        call_uuid = (
            gathered.get("call_uuid")
            or gathered.get("call_id")
            or message.get("call_uuid")
            or nested.get("call_uuid")
        )
        ctx = _log_ctx(org=organization_id, workflow=workflow_id, run=workflow_run_id)
        if not call_uuid:
            logger.error(f"{ctx} no call UUID for media websocket")
            await websocket.close(code=4400, reason="Missing call UUID")
            return

        logger.info(f"{ctx} call_uuid={call_uuid} starting pipeline")
        try:
            await run_pipeline_telephony(
                websocket,
                provider_name=self.PROVIDER_NAME,
                workflow_id=workflow_id,
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
                call_id=call_uuid,
                transport_kwargs={"call_uuid": call_uuid},
            )
        except Exception as e:
            logger.error(f"{ctx} call_uuid={call_uuid} pipeline failure: {e}")
            raise

    # ======== INBOUND CALL METHODS ========

    @classmethod
    def can_handle_webhook(
        cls, webhook_data: Dict[str, Any], headers: Dict[str, str]
    ) -> bool:
        """
        Determine if this provider can handle the incoming webhook.
        """
        claims = decode_unverified_claims(headers)
        if claims.get("iss") == "Vonage" and (
            claims.get("api_key") or claims.get("application_id")
        ):
            return True

        return bool(
            webhook_data.get("uuid")
            and webhook_data.get("conversation_uuid")
            and webhook_data.get("from")
            and webhook_data.get("to")
        )

    @staticmethod
    def parse_inbound_webhook(
        webhook_data: Dict[str, Any], headers: Optional[Dict[str, str]] = None
    ) -> NormalizedInboundData:
        """
        Parse Vonage-specific inbound webhook data into normalized format.

        ``account_id`` comes from the (not yet verified) signed JWT claims; it
        only selects the candidate configuration, whose signature secret then
        verifies the request.
        """
        claims = decode_unverified_claims(headers or {})
        direction = webhook_data.get("direction") or "inbound"
        status = webhook_data.get("status") or "started"

        return NormalizedInboundData(
            provider=VonageProvider.PROVIDER_NAME,
            call_id=webhook_data.get("uuid", ""),
            from_number=from_vonage_number(webhook_data.get("from")),
            to_number=from_vonage_number(webhook_data.get("to")),
            direction=direction,
            call_status=status,
            account_id=claims.get("api_key") or webhook_data.get("account_id"),
            from_country=None,
            to_country=None,
            raw_data=webhook_data,
        )

    # Kept for callers/tests that used the old private helpers.
    @staticmethod
    def _header(headers: Mapping[str, str], name: str) -> Optional[str]:
        from .auth import header

        return header(headers, name)

    @classmethod
    def _bearer_token(cls, headers: Mapping[str, str]) -> Optional[str]:
        return bearer_token(headers)

    @classmethod
    def _decode_unverified_signed_claims(
        cls, headers: Mapping[str, str]
    ) -> Dict[str, Any]:
        return decode_unverified_claims(headers)

    def _verify_signed_claims(
        self, headers: Mapping[str, str], body: str = ""
    ) -> Optional[Dict[str, Any]]:
        return verify_signed_jwt(
            headers,
            signature_secret=self.signature_secret,
            api_key=self.api_key,
            application_id=self.application_id,
            body=body,
        )

    @staticmethod
    def validate_account_id(config_data: dict, webhook_account_id: str) -> bool:
        """Validate Vonage account_id from webhook matches configuration"""
        if not webhook_account_id:
            return False

        stored_api_key = config_data.get("api_key")
        return stored_api_key == webhook_account_id

    async def verify_inbound_signature(
        self,
        url: str,
        webhook_data: Dict[str, Any],
        headers: Dict[str, str],
        body: str = "",
    ) -> bool:
        """
        Verify Vonage signed webhook JWT and payload hash. Fails closed.
        """
        claims = self._verify_signed_claims(headers, body)
        return claims is not None

    async def configure_inbound(
        self, address: str, webhook_url: Optional[str]
    ) -> ProviderSyncResult:
        """Point the Vonage Application's answer_url at Dograh's dispatcher.

        Vonage routes inbound calls per-application: a single ``answer_url`` on
        ``self.application_id`` applies to every number linked to it, and the
        same application may back several Dograh configurations. The
        ``address`` argument is informational.

        * Setting is idempotent: when the application already points at
          ``webhook_url`` with signed callbacks on, nothing is written, so
          several configurations sharing an application never fight over it.
        * Clearing (``webhook_url=None``) is a no-op on the Vonage side:
          unsetting the shared URL for one number would silently break inbound
          for every other number on the application. The DB-level disconnect
          is sufficient — calls to numbers without an inbound workflow are
          rejected by the dispatcher.

        Vonage's PUT /v2/applications/{id} is full-replacement, so we GET the
        current application, mutate the voice webhooks, and PUT it back using
        ``api_key``/``api_secret`` Basic auth (the Application API does not
        accept the Voice API JWT).
        """
        ctx = _log_ctx(application_id=self.application_id, address=address)
        if webhook_url is None:
            logger.info(
                f"{ctx} configure_inbound clear: skipping application update "
                "(answer_url is shared across all numbers on the application)"
            )
            return ProviderSyncResult(ok=True)

        if not (self.application_id and self.private_key):
            return ProviderSyncResult(
                ok=False, message="Vonage provider not properly configured"
            )

        if not (self.api_key and self.api_secret):
            return ProviderSyncResult(
                ok=False,
                message=(
                    "Vonage api_key and api_secret are required to update the "
                    "application's answer_url"
                ),
            )

        if not self.signature_secret:
            return ProviderSyncResult(
                ok=False,
                message=(
                    "Vonage signature_secret is required because inbound calls "
                    "use signed webhook verification"
                ),
            )

        app_endpoint = f"{self.base_url}/v2/applications/{self.application_id}"
        auth = aiohttp.BasicAuth(self.api_key, self.api_secret)

        try:
            async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
                async with session.get(app_endpoint, auth=auth) as response:
                    if response.status != 200:
                        body = (await response.text())[:300]
                        logger.error(
                            f"{ctx} application lookup failed: {response.status}"
                        )
                        return ProviderSyncResult(
                            ok=False,
                            message=self._application_api_error(response.status, body),
                        )
                    app_data = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.error(f"{ctx} application lookup error: {type(e).__name__}")
            return ProviderSyncResult(
                ok=False, message=f"Vonage lookup failed: {type(e).__name__}"
            )

        capabilities = app_data.get("capabilities") or {}
        voice = capabilities.get("voice") or {}
        webhooks = voice.get("webhooks") or {}
        backend_endpoint, _ = await get_backend_endpoints()
        event_url = f"{backend_endpoint}/api/v1/telephony/vonage/events"

        current_answer = (webhooks.get("answer_url") or {}).get("address")
        current_event = (webhooks.get("event_url") or {}).get("address")
        if (
            current_answer == webhook_url
            and current_event == event_url
            and voice.get("signed_callbacks") is True
        ):
            logger.info(f"{ctx} application already routed to Dograh; no update")
            return ProviderSyncResult(ok=True)

        if current_answer and current_answer != webhook_url:
            logger.warning(
                f"{ctx} replacing existing application answer_url "
                f"{current_answer!r}; every number linked to this application "
                "will now route to Dograh"
            )

        webhooks["answer_url"] = {"address": webhook_url, "http_method": "POST"}
        webhooks["event_url"] = {"address": event_url, "http_method": "POST"}
        voice["webhooks"] = webhooks
        voice["signed_callbacks"] = True
        capabilities["voice"] = voice

        update_body = {
            "name": app_data.get("name"),
            "capabilities": capabilities,
        }
        if "privacy" in app_data:
            update_body["privacy"] = app_data["privacy"]

        try:
            async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
                async with session.put(
                    app_endpoint, json=update_body, auth=auth
                ) as response:
                    if response.status not in (200, 201):
                        body = (await response.text())[:300]
                        logger.error(
                            f"{ctx} application update failed: {response.status}"
                        )
                        return ProviderSyncResult(
                            ok=False,
                            message=self._application_api_error(response.status, body),
                        )
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.error(f"{ctx} application update error: {type(e).__name__}")
            return ProviderSyncResult(
                ok=False, message=f"Vonage update failed: {type(e).__name__}"
            )

        logger.info(f"{ctx} answer_url set on application")
        return ProviderSyncResult(ok=True)

    @staticmethod
    def _application_api_error(status: int, body: str) -> str:
        if status == 401:
            return (
                "Vonage API 401: the API key/secret were rejected by the "
                "Application API"
            )
        if status == 404:
            return (
                "Vonage API 404: application not found for this API key. Check "
                "the Application ID belongs to the same account as the API key"
            )
        return f"Vonage API {status}: {body}"

    async def start_inbound_stream(
        self,
        *,
        websocket_url: str,
        workflow_run_id: int,
        normalized_data,
        backend_endpoint: str,
    ):
        """
        Generate NCCO response for inbound Vonage webhook.
        """
        from api.db import db_client

        workflow_run = await db_client.get_workflow_run_by_id(workflow_run_id)
        if not workflow_run or not workflow_run.workflow:
            raise ValueError(f"Workflow run {workflow_run_id} not found")

        ncco_response = [
            {
                "action": "connect",
                "eventUrl": [
                    f"{backend_endpoint}/api/v1/telephony/vonage/events/{workflow_run_id}"
                ],
                "eventMethod": "POST",
                "endpoint": [
                    self.build_websocket_endpoint(
                        websocket_url=websocket_url,
                        organization_id=workflow_run.workflow.organization_id,
                        workflow_id=workflow_run.workflow_id,
                        workflow_run_id=workflow_run_id,
                        telephony_configuration_id=(
                            workflow_run.initial_context or {}
                        ).get("telephony_configuration_id"),
                        call_uuid=normalized_data.call_id,
                    )
                ],
            }
        ]

        logger.info(
            f"{_log_ctx(org=workflow_run.workflow.organization_id, workflow=workflow_run.workflow_id, run=workflow_run_id, call_uuid=normalized_data.call_id, direction='inbound')} "
            "returning inbound NCCO"
        )
        return Response(
            content=json.dumps(ncco_response), media_type="application/json"
        )

    @staticmethod
    def _hangup_ncco(message: str) -> Response:
        ncco = [{"action": "talk", "text": message}, {"action": "hangup"}]
        return Response(content=json.dumps(ncco), media_type="application/json")

    @staticmethod
    def generate_error_response(error_type: str, message: str) -> tuple:
        """
        Generate a Vonage-specific error response.
        """
        return VonageProvider._hangup_ncco(
            f"Sorry, there was an error processing your call. {message}"
        )

    @staticmethod
    def generate_validation_error_response(error_type) -> tuple:
        """NCCO (not TwiML) for inbound validation failures."""
        from api.errors.telephony_errors import TELEPHONY_ERROR_MESSAGES, TelephonyError

        message = TELEPHONY_ERROR_MESSAGES.get(
            error_type, TELEPHONY_ERROR_MESSAGES[TelephonyError.GENERAL_AUTH_FAILED]
        )
        return VonageProvider._hangup_ncco(message)

    # ======== CALL TRANSFER METHODS ========

    def transfer_ncco(self, conference_name: str) -> List[Dict[str, Any]]:
        """NCCO that places a leg into the transfer conversation.

        ``endOnExit`` on both legs: when either party hangs up, the
        conversation (and with it the other leg) ends — no orphaned legs.
        """
        return [
            {
                "action": "conversation",
                "name": conference_name,
                "startOnEnter": True,
                "endOnExit": True,
            }
        ]

    async def transfer_call(
        self,
        destination: str,
        transfer_id: str,
        conference_name: str,
        timeout: int = 30,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Dial the transfer destination into a named Vonage conversation.

        Mirrors Dograh's conference transfer model (see Twilio): the
        destination leg is created with an inline NCCO that joins
        ``conference_name`` when answered. Its lifecycle events go to
        ``/vonage/transfer-events/{transfer_id}``, which publishes
        DESTINATION_ANSWERED / TRANSFER_FAILED. On DESTINATION_ANSWERED the
        pipeline ends with ``TRANSFER_CALL`` and the serializer's transfer
        strategy moves the caller leg into the same conversation.
        ``ringing_timer`` makes Vonage itself stop ringing at ``timeout``.
        """
        if not self.validate_config():
            raise VonageAPIError(
                VonageErrorCategory.NOT_CONFIGURED,
                provider_detail="Application ID, private key and a caller ID are required",
                operation="transfer",
            )

        caller = self._select_caller_number(kwargs.pop("from_number", None))
        try:
            vonage_to = to_vonage_number(destination, field="transfer destination")
            vonage_from = to_vonage_number(caller, field="caller ID")
        except VonagePhoneNumberError as exc:
            raise VonageAPIError(
                VonageErrorCategory.INVALID_NUMBER,
                provider_detail=str(exc),
                operation="transfer",
            ) from None

        backend_endpoint, _ = await get_backend_endpoints()
        ringing_timer = max(_MIN_RINGING_TIMER, min(int(timeout), _MAX_RINGING_TIMER))
        data = {
            "to": [{"type": "phone", "number": vonage_to}],
            "from": {"type": "phone", "number": vonage_from},
            "ncco": [
                {
                    "action": "talk",
                    "text": "You have answered a transfer call. Connecting you now.",
                },
                *self.transfer_ncco(conference_name),
            ],
            "event_url": [
                f"{backend_endpoint}/api/v1/telephony/vonage/transfer-events/{transfer_id}"
            ],
            "event_method": "POST",
            "ringing_timer": ringing_timer,
        }

        ctx = _log_ctx(transfer_id=transfer_id, conference=conference_name)
        logger.info(
            f"{ctx} dialing transfer destination to=***{vonage_to[-4:]} "
            f"ringing_timer={ringing_timer}"
        )
        response_data = await self.client.create_call(data)
        call_uuid = response_data["uuid"]
        logger.info(f"{ctx} transfer leg call_uuid={call_uuid}")
        return {
            "call_sid": call_uuid,
            "status": response_data.get("status", "started"),
            "provider": self.PROVIDER_NAME,
            "from_number": to_e164(caller, field="caller ID"),
            "to_number": f"+{vonage_to}",
            "raw_response": response_data,
        }

    async def cancel_transfer_call(self, transfer_call_id: str) -> None:
        """Hang up a transfer destination leg that is no longer wanted."""
        if not transfer_call_id:
            return
        try:
            await self.client.hangup(transfer_call_id)
            logger.info(
                f"{_log_ctx(call_uuid=transfer_call_id)} transfer leg cancelled"
            )
        except Exception as e:
            logger.error(
                f"{_log_ctx(call_uuid=transfer_call_id)} failed to cancel transfer "
                f"leg: {e}"
            )

    def supports_transfers(self) -> bool:
        """
        Vonage supports call transfers via named conversations.
        """
        return True
