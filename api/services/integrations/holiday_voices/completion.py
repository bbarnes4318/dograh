"""Post-call report to the Holiday Voices store in Hoppwhistle.

Runs in the workflow-completion job (never in the live call path), once per
enabled ``holiday_voices`` node, for runs Hoppwhistle opened a session for:
the run's ``initial_context`` carries ``hv_session_id`` / ``hv_session_token``
(the pre-call fetch answer for inbound calls, the call's context for
outbound ones). A run without them is not the store's and is skipped.

Recordings and transcripts stay in Dograh (MinIO). Only links are sent: the
public-token download URLs Dograh mints for post-call work, so the store's
staff open them through Dograh's own access path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger

from api.constants import BACKEND_API_ENDPOINT
from api.services.integrations.base import IntegrationCompletionContext

from .client import deliver_report
from .node import HolidayVoicesNodeData
from .tool_catalog import CONTRACT_VERSION, completion_url

_INTERNAL_KEYS = frozenset(
    {
        "nodes_visited",
        "node_path",
        "trace_url",
        "call_id",
        "extracted_variables",
        "call_disposition",
        "mapped_call_disposition",
        "call_tags",
        "transfer_attempts",
        "external_pbx_transferred",
    }
)
_VOICEMAIL_MARKERS = ("voicemail", "answering_bot", "machine")


def _public_urls(
    context: IntegrationCompletionContext,
) -> tuple[str | None, str | None]:
    run = context.workflow_run
    if context.public_token:
        base = f"{BACKEND_API_ENDPOINT}/api/v1/public/download/workflow/{context.public_token}"
        return (
            f"{base}/recording" if run.recording_url else None,
            f"{base}/transcript" if run.transcript_url else None,
        )
    return None, None  # never a raw storage key


def _extracted(gathered: dict[str, Any]) -> dict[str, Any]:
    nested = gathered.get("extracted_variables")
    source = nested if isinstance(nested, dict) else gathered
    return {
        str(key): value
        for key, value in source.items()
        if key not in _INTERNAL_KEYS
        and isinstance(value, (str, int, float, bool))
        and not str(key).startswith("_")
        and not str(key).startswith("hv_session")
    }


def _tool_calls(run: Any) -> list[str]:
    """Names of the tools the agent called, in order, from the run's logs."""
    logs = run.logs if isinstance(getattr(run, "logs", None), dict) else {}
    events = logs.get("realtime_feedback_events") or []
    names: list[str] = []
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict):
            continue
        if event.get("type") not in ("rtf-function-call-start", "function_call_start"):
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        name = payload.get("function_name") or payload.get("name")
        if isinstance(name, str):
            names.append(name[:60])
    return names[:200]


def _summary(run: Any) -> str | None:
    annotations = (
        run.annotations if isinstance(getattr(run, "annotations", None), dict) else {}
    )
    for value in annotations.values():
        if isinstance(value, dict) and isinstance(value.get("summary"), str):
            text = value["summary"].strip()
            if text:
                return text[:4000]
    return None


def build_report(
    context: IntegrationCompletionContext, node: HolidayVoicesNodeData
) -> dict[str, Any] | None:
    """The report for this run, or None when the store did not open a session for it."""
    run = context.workflow_run
    initial = run.initial_context or {}
    session_id = initial.get("hv_session_id")
    session_token = initial.get("hv_session_token")
    if not session_id or not session_token:
        return None

    gathered = run.gathered_context or {}
    usage = run.usage_info or {}
    disposition = gathered.get("call_disposition") or gathered.get(
        "mapped_call_disposition"
    )
    duration = usage.get("call_duration_seconds")
    duration = float(duration) if isinstance(duration, (int, float)) else None
    latency = (usage.get("latency") or {}).get("avg_seconds")
    started_at = run.created_at
    ended_at = (
        started_at + timedelta(seconds=duration)
        if started_at and duration is not None
        else None
    )
    recording_url, transcript_url = _public_urls(context)
    voicemail = bool(disposition) and any(
        marker in str(disposition).lower() for marker in _VOICEMAIL_MARKERS
    )
    cost_cents = None
    if node.holiday_voices_cost_per_minute_cents is not None and duration is not None:
        cost_cents = round(duration / 60 * node.holiday_voices_cost_per_minute_cents)
    attempts = gathered.get("transfer_attempts")

    return {
        "contract_version": CONTRACT_VERSION,
        "session_id": str(session_id),
        "session_token": str(session_token),
        "run_id": str(context.workflow_run_id),
        "workflow_id": str(run.workflow_id),
        "agent": node.holiday_voices_agent,
        "outcome": str(disposition)[:60] if disposition else None,
        "summary": _summary(run),
        "extracted": _extracted(gathered),
        "tools_used": _tool_calls(run),
        "transfer_attempts": attempts if isinstance(attempts, list) else [],
        "recording_url": recording_url,
        "transcript_url": transcript_url,
        "started_at": started_at.astimezone(UTC).isoformat() if started_at else None,
        "ended_at": ended_at.astimezone(UTC).isoformat() if ended_at else None,
        "duration_seconds": round(duration) if duration is not None else None,
        "voicemail": voicemail,
        "latency_ms_avg": round(latency * 1000)
        if isinstance(latency, (int, float))
        else None,
        "cost_cents": cost_cents,
    }


async def _auth_headers(
    credential_uuid: str, organization_id: int
) -> dict[str, str] | None:
    from api.db import db_client
    from api.utils.credential_auth import build_auth_header

    credential = await db_client.get_credential_by_uuid(
        credential_uuid, organization_id
    )
    if not credential:
        return None
    return build_auth_header(credential)


async def run_completion(
    nodes: list[dict[str, Any]], context: IntegrationCompletionContext
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for node in nodes:
        node_id = node.get("id", "unknown")
        key = f"holiday_voices_{node_id}"
        try:
            data = HolidayVoicesNodeData.model_validate(node.get("data", {}))
        except Exception as exc:
            logger.warning(f"Holiday Voices node #{node_id} failed validation: {exc}")
            results[key] = {"error": "validation_failed"}
            continue
        if not data.holiday_voices_enabled:
            continue
        report = build_report(context, data)
        if report is None:
            results[key] = {"skipped": "not_a_holiday_voices_session"}
            continue
        # Credential is looked up inside this run's organization only.
        headers = await _auth_headers(
            data.holiday_voices_credential_uuid or "", context.organization_id
        )
        if not headers:
            results[key] = {"delivered": False, "error": "credential_not_found"}
            continue
        delivery = await deliver_report(
            url=completion_url(data.holiday_voices_base_url or ""),
            auth_headers=headers,
            payload=report,
        )
        if not delivery.ok:
            logger.warning(
                f"Holiday Voices report for run {context.workflow_run_id} not delivered: "
                f"{delivery.status_code} {delivery.detail}"
            )
        results[key] = {
            "delivered": delivery.ok,
            "status_code": delivery.status_code,
            "at": datetime.now(UTC).isoformat(),
        }
    return results
