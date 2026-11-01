"""Every finished call, to Hoppwhistle, the moment it ends.

``POST {HOPPWHISTLE_LEAD_INTEL_URL}/api/internal/lead-intel/dograh/runs``,
body contract ``dograh-run-v1``, signed like call-quality ingestion
(``X-Hopwhistle-Signature: v1=<HMAC-SHA256(secret, "{ts}.{body}")>`` beside
``X-Hopwhistle-Timestamp``) with ``DOGRAH_QUALITY_INGEST_SECRET``.

This is how a real estate workspace's agents see what the voice agent
captured -- the extracted fields, the disposition, the transcript and the
recording -- on their dialer and softphone as soon as the call ends,
including when the voice agent transfers the caller to them live.

Hoppwhistle, not this module, decides whose run it is: the workflow's
binding there names the tenant, an unbound workflow is quarantined and an
insurance one is only graded. So every completed run is sent, and nothing
here knows about tenants.

Runs in the workflow-completion job, never in the live call path. Off unless
both environment variables are set. A failure is logged and dropped: the
call has already happened, and Hoppwhistle's ingestion is idempotent on the
run id, so the call-quality bridge can always catch up later.
"""

from __future__ import annotations

import os
from datetime import UTC, timedelta
from typing import Any

from api.constants import BACKEND_API_ENDPOINT
from loguru import logger

from .client import DeliveryResult, deliver_report
from .completion import _INTERNAL_KEYS

LEAD_INTEL_PATH = "/api/internal/lead-intel/dograh/runs"
URL_ENV = "HOPPWHISTLE_LEAD_INTEL_URL"
SECRET_ENV = "DOGRAH_QUALITY_INGEST_SECRET"

_MAX_TEXT = 2000
_MAX_TRANSCRIPT = 200_000


def lead_intel_config(env: dict[str, str] | None = None) -> tuple[str, str] | None:
    """(base url, secret), or None when delivery is off."""
    source = env if env is not None else os.environ
    url = (source.get(URL_ENV) or "").strip().rstrip("/")
    secret = (source.get(SECRET_ENV) or "").strip()
    if not url or not secret:
        return None
    return url, secret


def _text(value: Any, limit: int = _MAX_TEXT) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _extracted(gathered: dict[str, Any]) -> dict[str, Any]:
    """The fields the voice agent captured, as primitives Hoppwhistle accepts."""
    nested = gathered.get("extracted_variables")
    out: dict[str, Any] = {}
    for source in (gathered, nested if isinstance(nested, dict) else {}):
        for key, value in source.items():
            name = str(key)
            if name in _INTERNAL_KEYS or name.startswith("_") or len(name) > 200:
                continue
            if value is None or isinstance(value, (bool, int, float)):
                out[name] = value
            elif isinstance(value, str):
                out[name] = value[:_MAX_TEXT]
    return out


def _call_parties(
    initial: dict[str, Any], call_type: str
) -> tuple[str | None, str | None]:
    """(customer number, our number) for the call, as call setup recorded them."""
    caller = _text(initial.get("caller_number"), 40)
    called = _text(initial.get("called_number") or initial.get("phone_number"), 40)
    inbound = (
        call_type == "inbound"
        or str(initial.get("direction") or "").lower() == "inbound"
    )
    return (caller, called) if inbound else (called, caller)


def build_lead_intel_payload(
    run: Any, public_token: str | None = None
) -> dict[str, Any]:
    """The ``dograh-run-v1`` body for a completed workflow run."""
    initial = run.initial_context or {}
    gathered = run.gathered_context or {}
    usage = run.usage_info or {}

    call_type_raw = getattr(run, "call_type", None)
    call_type = str(
        getattr(call_type_raw, "value", call_type_raw) or "outbound"
    ).lower()
    if str(initial.get("direction") or "").lower() == "inbound":
        call_type = "inbound"
    if call_type not in ("inbound", "outbound"):
        call_type = "outbound"
    customer, ours = _call_parties(initial, call_type)

    duration = usage.get("call_duration_seconds")
    duration = float(duration) if isinstance(duration, (int, float)) else None
    started = run.created_at
    ended = (
        started + timedelta(seconds=duration)
        if started and duration is not None
        else None
    )

    recording_url, transcript_url = run.recording_url, run.transcript_url
    if public_token:
        base = f"{BACKEND_API_ENDPOINT}/api/v1/public/download/workflow/{public_token}"
        recording_url = f"{base}/recording" if run.recording_url else None
        transcript_url = f"{base}/transcript" if run.transcript_url else None

    disposition = gathered.get("call_disposition") or gathered.get(
        "mapped_call_disposition"
    )
    attempts = gathered.get("transfer_attempts")
    attempts = attempts if isinstance(attempts, list) else []
    transferred = bool(gathered.get("external_pbx_transferred")) or any(
        isinstance(a, dict)
        and str(a.get("status") or "").lower() in ("success", "completed", "bridged")
        for a in attempts
    )
    routing = initial.get("inbound_routing")
    nodes = gathered.get("node_path") or gathered.get("nodes_visited") or []

    return {
        "externalRunId": str(run.id),
        "workflowId": str(run.workflow_id),
        "workflowName": _text(
            getattr(getattr(run, "workflow", None), "name", None), 300
        ),
        "campaignId": str(run.campaign_id) if run.campaign_id is not None else None,
        "callType": call_type,
        "customerNumber": customer,
        "ourNumber": ours,
        "aiTransfer": transferred,
        "inboundRouting": {
            str(k)[:200]: v
            for k, v in routing.items()
            if v is None or isinstance(v, (str, int, float, bool))
        }
        if isinstance(routing, dict)
        else None,
        "hwSessionId": _text(initial.get("hw_session_id"), 200),
        "startedAt": started.astimezone(UTC).isoformat() if started else None,
        "endedAt": ended.astimezone(UTC).isoformat() if ended else None,
        "durationSeconds": min(duration, 36_000) if duration is not None else None,
        "disposition": _text(disposition, 200),
        "transferState": _text(gathered.get("transfer_state"), 200),
        "callTags": [str(t)[:200] for t in (gathered.get("call_tags") or [])][:50],
        "nodesVisited": [str(n)[:200] for n in nodes if n is not None][:100]
        if isinstance(nodes, list)
        else [],
        "extracted": _extracted(gathered),
        "transferAttempts": attempts[:50],
        "transcriptText": (run.transcript_text or "")[:_MAX_TRANSCRIPT],
        "recordingUrl": recording_url,
        "transcriptUrl": transcript_url,
    }


async def deliver_lead_intel(
    run: Any,
    public_token: str | None = None,
    env: dict[str, str] | None = None,
) -> DeliveryResult | None:
    """Sends the run. None when delivery is off or the run cannot be described."""
    config = lead_intel_config(env)
    if not config:
        return None
    if not run.created_at:
        return None
    base_url, secret = config
    payload = build_lead_intel_payload(run, public_token)
    result = await deliver_report(
        base_url=base_url,
        secret=secret,
        payload=payload,
        path=LEAD_INTEL_PATH,
    )
    if not result.ok:
        logger.warning(
            f"Lead-intel delivery for run {run.id} failed: {result.status_code} {result.detail}"
        )
    return result


async def deliver_lead_intel_for_run(workflow_run_id: int) -> DeliveryResult | None:
    """Loads the completed run and sends it. For the workflow-completion job."""
    if not lead_intel_config():
        return None
    from api.db import db_client

    run, _organization_id = await db_client.get_workflow_run_with_context(
        workflow_run_id
    )
    if not run or not run.is_completed:
        return None
    token = None
    if run.recording_url or run.transcript_url:
        token = await db_client.ensure_public_access_token(workflow_run_id)
    return await deliver_lead_intel(run, token)
