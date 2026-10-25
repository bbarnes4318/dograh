"""Post-call report to Hoppwhistle.

Runs in the workflow-completion job (never in the live call path), once per
enabled ``hoppwhistle`` node, for runs Hoppwhistle started: the run's
``initial_context`` carries ``hw_session_id`` / ``hw_session_token`` (put there
by Hoppwhistle's outbound trigger or its pre-call fetch answer). A run without
them is not Hoppwhistle's and is skipped.

What is sent is the conversation's own facts -- disposition, transcript,
recording and transcript links, transfer attempts, extracted variables,
usage, latency -- and nothing Hoppwhistle could not be told. Hoppwhistle
decides the outcome, retries and CRM writes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger

from api.constants import BACKEND_API_ENDPOINT
from api.services.integrations.base import IntegrationCompletionContext

from .client import deliver_report
from .node import HoppwhistleNodeData
from .tool_catalog import CONTRACT_VERSION

#: Gathered-context keys that are engine bookkeeping, not facts about the prospect.
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

_VOICEMAIL_DISPOSITIONS = ("voicemail", "answering_bot", "machine")


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
    return run.recording_url, run.transcript_url


def _extracted(gathered: dict[str, Any]) -> dict[str, Any]:
    nested = gathered.get("extracted_variables")
    source = nested if isinstance(nested, dict) else gathered
    return {
        key: value
        for key, value in source.items()
        if key not in _INTERNAL_KEYS
        and isinstance(value, (str, int, float, bool))
        and not str(key).startswith("_")
    }


def build_report(
    context: IntegrationCompletionContext,
    node: HoppwhistleNodeData,
) -> dict[str, Any] | None:
    """The report for this run, or None when Hoppwhistle did not start it."""
    run = context.workflow_run
    initial = run.initial_context or {}
    session_id = initial.get("hw_session_id")
    session_token = initial.get("hw_session_token")
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
        marker in str(disposition).lower() for marker in _VOICEMAIL_DISPOSITIONS
    )

    cost_cents = None
    if node.hoppwhistle_cost_per_minute_cents is not None and duration is not None:
        cost_cents = round(duration / 60 * node.hoppwhistle_cost_per_minute_cents, 2)

    attempts = gathered.get("transfer_attempts")
    return {
        "contract_version": CONTRACT_VERSION,
        "session_id": str(session_id),
        "session_token": str(session_token),
        "external_run_id": str(context.workflow_run_id),
        "workflow_id": str(run.workflow_id),
        "disposition": str(disposition) if disposition else None,
        "call_tags": [str(t) for t in (gathered.get("call_tags") or [])][:50],
        "extracted": _extracted(gathered),
        "transfer_attempts": attempts if isinstance(attempts, list) else [],
        "transcript_text": run.transcript_text,
        "recording_url": recording_url,
        "transcript_url": transcript_url,
        "started_at": started_at.astimezone(UTC).isoformat() if started_at else None,
        "ended_at": ended_at.astimezone(UTC).isoformat() if ended_at else None,
        "duration_seconds": duration,
        "voicemail": voicemail,
        "latency_ms_avg": round(latency * 1000)
        if isinstance(latency, (int, float))
        else None,
        "cost_cents": cost_cents,
        "usage": {key: usage.get(key) for key in ("llm", "tts", "stt") if key in usage},
    }


async def run_completion(
    nodes: list[dict[str, Any]],
    context: IntegrationCompletionContext,
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for node in nodes:
        node_id = node.get("id", "unknown")
        try:
            data = HoppwhistleNodeData.model_validate(node.get("data", {}))
        except Exception as exc:
            logger.warning(
                f"Hoppwhistle node #{node_id} failed validation, skipping: {exc}"
            )
            results[f"hoppwhistle_{node_id}"] = {"error": "validation_failed"}
            continue
        if not data.hoppwhistle_enabled:
            continue

        report = build_report(context, data)
        if report is None:
            results[f"hoppwhistle_{node_id}"] = {"skipped": "not_a_hoppwhistle_session"}
            continue

        delivery = await deliver_report(
            base_url=data.hoppwhistle_base_url or "",
            secret=data.hoppwhistle_signing_secret or "",
            payload=report,
        )
        if not delivery.ok:
            logger.warning(
                f"Hoppwhistle report for run {context.workflow_run_id} not delivered: "
                f"{delivery.status_code} {delivery.detail}"
            )
        # Never the token or the transcript: what happened, and when.
        results[f"hoppwhistle_{node_id}"] = {
            "delivered": delivery.ok,
            "status_code": delivery.status_code,
            "at": datetime.now(UTC).isoformat(),
        }
    return results
