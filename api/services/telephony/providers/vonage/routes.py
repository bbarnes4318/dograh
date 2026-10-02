"""Vonage telephony routes (webhooks, status callbacks, answer URLs).

Mounted under ``/api/v1/telephony`` by ``api.routes.telephony`` via the
provider registry — see ProviderSpec.router.

Every route verifies Vonage's signed-callback JWT against the configuration
that owns the workflow run *before* acting on the payload, and fails closed.
"""

import json
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from loguru import logger
from pipecat.utils.run_context import set_current_run_id

from api.db import db_client
from api.services.telephony.call_transfer_manager import get_call_transfer_manager
from api.services.telephony.factory import get_telephony_provider_for_run
from api.services.telephony.transfer_event_protocol import (
    TransferEvent,
    TransferEventType,
)

router = APIRouter()


def _ctx(**fields) -> str:
    parts = ["provider=vonage"]
    parts.extend(f"{k}={v}" for k, v in fields.items() if v is not None)
    return " ".join(parts)


@router.get("/ncco", include_in_schema=False)
async def handle_ncco_webhook(
    workflow_id: int,
    workflow_run_id: int,
    organization_id: int,
    request: Request,
    uuid: Optional[str] = None,
):
    """Answer URL for outbound Vonage calls; returns the NCCO JSON.

    The request must be Vonage-signed by the run's configuration and the
    workflow/organization in the query string must match the run, so a
    third party cannot fetch an NCCO (and its media token) for a run.
    """
    set_current_run_id(workflow_run_id)
    ctx = _ctx(
        org=organization_id, workflow=workflow_id, run=workflow_run_id, call_uuid=uuid
    )

    workflow_run = await db_client.get_workflow_run(
        workflow_run_id, organization_id=organization_id
    )
    if not workflow_run or workflow_run.workflow_id != workflow_id:
        logger.warning(f"{ctx} answer webhook for unknown run/workflow/org")
        raise HTTPException(status_code=404, detail="Workflow run not found")

    provider = await get_telephony_provider_for_run(workflow_run, organization_id)
    if provider.PROVIDER_NAME != "vonage":
        raise HTTPException(status_code=400, detail="Provider mismatch")

    if not await provider.verify_inbound_signature(
        str(request.url), dict(request.query_params), dict(request.headers), ""
    ):
        logger.warning(f"{ctx} answer webhook rejected: invalid signature")
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    response_content = await provider.get_webhook_response(
        workflow_id,
        organization_id,
        workflow_run_id,
        telephony_configuration_id=(workflow_run.initial_context or {}).get(
            "telephony_configuration_id"
        ),
        call_uuid=uuid,
    )
    logger.info(f"{ctx} returning outbound NCCO")
    return json.loads(response_content)


async def _read_json_body(request: Request) -> tuple[dict, str]:
    body_bytes = await request.body()
    try:
        raw_body = body_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=400, detail="Webhook body is not valid UTF-8"
        ) from exc
    try:
        data = json.loads(raw_body or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Webhook body is not JSON") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Webhook body must be an object")
    return data, raw_body


def _is_duplicate_event(workflow_run, event_data: dict) -> bool:
    """Same leg/status/timestamp already recorded (Vonage retry or replay)."""
    timestamp = event_data.get("timestamp")
    if not timestamp:
        return False
    callbacks = (workflow_run.logs or {}).get("telephony_status_callbacks", [])
    for entry in callbacks:
        if (
            isinstance(entry, dict)
            and entry.get("uuid") == event_data.get("uuid")
            and entry.get("timestamp_vonage") == timestamp
            and str(entry.get("vonage_status", "")).lower()
            == str(event_data.get("status", "")).lower()
        ):
            return True
    return False


async def _persist_amd_result(provider, workflow_run_id: int, event_data: dict) -> bool:
    amd_result = provider.parse_answering_machine_detection_result(event_data)
    if not amd_result:
        return False
    logger.info(
        f"{_ctx(run=workflow_run_id, call_uuid=amd_result.call_id)} "
        f"AMD result answered_by={amd_result.answered_by}"
    )
    try:
        await db_client.update_workflow_run(
            run_id=workflow_run_id,
            gathered_context={"answered_by": amd_result.answered_by},
        )
    except Exception as exc:
        logger.warning(f"[run {workflow_run_id}] Failed to persist AMD result: {exc}")
    return True


async def _handle_vonage_event_request(request: Request, workflow_run_id: int):
    set_current_run_id(workflow_run_id)
    event_data, raw_body = await _read_json_body(request)

    workflow_run = await db_client.get_workflow_run_by_id(workflow_run_id)
    if not workflow_run:
        logger.error(f"[run {workflow_run_id}] Workflow run not found")
        return {"status": "error", "message": "Workflow run not found"}

    workflow = await db_client.get_workflow_by_id(workflow_run.workflow_id)
    if not workflow:
        logger.error(f"[run {workflow_run_id}] Workflow not found")
        return {"status": "error", "message": "Workflow not found"}

    provider = await get_telephony_provider_for_run(
        workflow_run, workflow.organization_id
    )
    signature_valid = await provider.verify_inbound_signature(
        str(request.url), event_data, dict(request.headers), raw_body
    )
    if not signature_valid:
        logger.warning(
            f"{_ctx(org=workflow.organization_id, run=workflow_run_id)} "
            "event rejected: invalid signature"
        )
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    gathered = workflow_run.gathered_context or {}
    event_uuid = event_data.get("uuid")
    vonage_status = event_data.get("status")
    ctx = _ctx(
        org=workflow.organization_id,
        cfg=(workflow_run.initial_context or {}).get("telephony_configuration_id"),
        workflow=workflow_run.workflow_id,
        run=workflow_run_id,
        campaign=getattr(workflow_run, "campaign_id", None),
        call_uuid=event_uuid,
        direction=event_data.get("direction"),
    )

    if await _persist_amd_result(provider, workflow_run_id, event_data):
        return {"status": "ok"}

    # Only the run's own call leg drives its lifecycle. The media websocket
    # leg (and any other leg in the conversation) reports to the same event
    # URL; its "completed" must not end the run while the caller is still on.
    primary_call_id = gathered.get("call_id") or gathered.get("call_uuid")
    if primary_call_id and event_uuid and event_uuid != primary_call_id:
        logger.info(f"{ctx} ignoring event for secondary leg state={vonage_status}")
        return {"status": "ok", "ignored": "secondary_leg"}

    if not vonage_status:
        logger.info(f"{ctx} ignoring non-status event keys={sorted(event_data)[:8]}")
        return {"status": "ok"}

    if _is_duplicate_event(workflow_run, event_data):
        logger.info(f"{ctx} duplicate event state={vonage_status}; skipping")
        return {"status": "ok", "duplicate": True}

    from api.services.telephony.status_processor import (
        StatusCallbackRequest,
        _process_status_update,
    )

    parsed_data = provider.parse_status_callback(event_data)
    if parsed_data["status"] is None:
        logger.info(f"{ctx} non-lifecycle event state={vonage_status}")
        return {"status": "ok"}

    status_value = getattr(parsed_data["status"], "value", parsed_data["status"])
    logger.info(f"{ctx} state={vonage_status} lifecycle={status_value}")

    extra = dict(parsed_data.get("extra", {}))
    # Keep Vonage's own status/timestamp alongside the normalized log entry
    # (the processor overwrites ``status``/``timestamp``) for dedupe/audit.
    extra["vonage_status"] = vonage_status
    if event_data.get("timestamp"):
        extra["timestamp_vonage"] = event_data["timestamp"]
    extra.pop("status", None)
    extra.pop("timestamp", None)

    status_update = StatusCallbackRequest(
        call_id=parsed_data["call_id"],
        status=parsed_data["status"],
        from_number=parsed_data.get("from_number"),
        to_number=parsed_data.get("to_number"),
        direction=parsed_data.get("direction"),
        duration=parsed_data.get("duration"),
        extra=extra,
    )

    await _process_status_update(workflow_run_id, status_update)
    return {"status": "ok"}


@router.post("/vonage/events/{workflow_run_id}")
async def handle_vonage_events(
    request: Request,
    workflow_run_id: int,
):
    """Handle Vonage-specific event webhooks.

    Vonage sends all call events to a single endpoint.
    Events include: started, ringing, answered, complete, failed, etc.
    """
    return await _handle_vonage_event_request(request, workflow_run_id)


@router.post("/vonage/events")
async def handle_vonage_events_without_run(request: Request):
    """Handle application-level events by resolving the run from call UUID."""
    event_data, _ = await _read_json_body(request)
    call_id = event_data.get("uuid")
    if call_id:
        workflow_run = await db_client.get_workflow_run_by_call_id(call_id)
        if workflow_run:
            return await _handle_vonage_event_request(request, workflow_run.id)

    logger.info(
        "Received unmatched Vonage application event "
        f"uuid={event_data.get('uuid')} status={event_data.get('status')}"
    )
    return {"status": "ok"}


# Transfer destination leg states -> generic transfer failure reasons.
_TRANSFER_FAILURE_REASONS = {
    "busy": (
        "busy",
        "The transfer call encountered a busy signal. The person is likely on another call.",
    ),
    "timeout": (
        "no_answer",
        "The transfer call was not answered. The person may be busy or unavailable right now.",
    ),
    "unanswered": (
        "no_answer",
        "The transfer call was not answered. The person may be busy or unavailable right now.",
    ),
    "rejected": (
        "call_failed",
        "The transfer call failed to connect. There may be a network issue or the number is unavailable.",
    ),
    "failed": (
        "call_failed",
        "The transfer call failed to connect. There may be a network issue or the number is unavailable.",
    ),
    "cancelled": (
        "call_failed",
        "The transfer call was cancelled before it connected.",
    ),
}


@router.post("/vonage/transfer-events/{transfer_id}")
async def handle_vonage_transfer_events(transfer_id: str, request: Request):
    """Lifecycle events of a transfer destination leg.

    ``answered`` publishes DESTINATION_ANSWERED (the pipeline then ends with
    TRANSFER_CALL and the caller is moved into the conversation); terminal
    non-answer states publish TRANSFER_FAILED so the agent can recover.
    """
    event_data, raw_body = await _read_json_body(request)
    status = str(event_data.get("status") or "").lower()
    leg_uuid = event_data.get("uuid")

    call_transfer_manager = await get_call_transfer_manager()
    transfer_context = await call_transfer_manager.get_transfer_context(transfer_id)
    ctx = _ctx(transfer_id=transfer_id, call_uuid=leg_uuid, state=status or None)

    if not transfer_context:
        # The transfer is over (bridged, timed out, or the caller left). The
        # paths that drop the context already hung up any leftover leg
        # (``cancel_transfer_call`` / the hangup strategy). Without a context
        # there is no configuration to verify the signature against, so this
        # unauthenticated request is acknowledged and otherwise ignored.
        logger.info(f"{ctx} no transfer context; ignoring")
        return {"status": "ignored"}

    workflow_run = (
        await db_client.get_workflow_run_by_id(transfer_context.workflow_run_id)
        if transfer_context.workflow_run_id
        else None
    )
    if not workflow_run or not workflow_run.workflow:
        logger.warning(f"{ctx} transfer context has no resolvable workflow run")
        raise HTTPException(status_code=404, detail="Transfer not found")

    provider = await get_telephony_provider_for_run(
        workflow_run, workflow_run.workflow.organization_id
    )
    if not await provider.verify_inbound_signature(
        str(request.url), event_data, dict(request.headers), raw_body
    ):
        logger.warning(f"{ctx} transfer event rejected: invalid signature")
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    # Events for a different leg than the one we dialed are not ours.
    if transfer_context.call_sid and leg_uuid and leg_uuid != transfer_context.call_sid:
        logger.info(f"{ctx} ignoring event for unrelated leg")
        return {"status": "ignored"}

    original_call_sid = transfer_context.original_call_sid or ""
    conference_name = transfer_context.conference_name

    if status == "answered":
        transfer_event = TransferEvent(
            type=TransferEventType.DESTINATION_ANSWERED,
            transfer_id=transfer_id,
            original_call_sid=original_call_sid,
            transfer_call_sid=leg_uuid,
            conference_name=conference_name,
            status="success",
            action="destination_answered",
            message="Great! The destination number answered. Let me transfer you now.",
        )
    elif status in _TRANSFER_FAILURE_REASONS:
        reason, message = _TRANSFER_FAILURE_REASONS[status]
        transfer_event = TransferEvent(
            type=TransferEventType.TRANSFER_FAILED,
            transfer_id=transfer_id,
            original_call_sid=original_call_sid,
            transfer_call_sid=leg_uuid,
            conference_name=conference_name,
            status="transfer_failed",
            action="transfer_failed",
            reason=reason,
            message=message,
            end_call=True,
        )
    else:
        # started / ringing / completed after a successful bridge.
        logger.info(f"{ctx} transfer leg state; no outcome")
        return {"status": "pending"}

    logger.info(f"{ctx} publishing {transfer_event.type.value}")
    await call_transfer_manager.publish_transfer_event(transfer_event)
    return {"status": "completed"}
