"""Terminal-status idempotency and out-of-order protection in the shared
status processor (provider-agnostic)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from api.enums import TelephonyCallStatus, WorkflowRunState
from api.services.telephony import status_processor
from api.services.telephony.status_processor import (
    StatusCallbackRequest,
    _process_status_update,
)


@pytest.fixture
def processor_env():
    state = {"logs": {"telephony_status_callbacks": []}, "state": "running"}

    def _run():
        return SimpleNamespace(
            id=1,
            logs={
                "telephony_status_callbacks": list(
                    state["logs"]["telephony_status_callbacks"]
                )
            },
            state=state["state"],
            is_completed=state["state"] == WorkflowRunState.COMPLETED.value,
            campaign_id=42,
            queued_run_id=9,
            gathered_context={},
        )

    async def update_workflow_run(run_id, **kwargs):
        if "logs" in kwargs:
            state["logs"] = kwargs["logs"]
        if "state" in kwargs:
            state["state"] = kwargs["state"]

    db = SimpleNamespace(
        get_workflow_run_by_id=AsyncMock(side_effect=lambda _id: _run()),
        update_workflow_run=AsyncMock(side_effect=update_workflow_run),
        record_telephony_duration=AsyncMock(),
    )
    dispatcher = SimpleNamespace(release_call_slot=AsyncMock())
    breaker = SimpleNamespace(record_and_evaluate=AsyncMock())
    publisher = SimpleNamespace(publish_retry_needed=AsyncMock())
    outcome = AsyncMock()

    with (
        patch.object(status_processor, "db_client", db),
        patch.object(status_processor, "campaign_call_dispatcher", dispatcher),
        patch.object(status_processor, "circuit_breaker", breaker),
        patch.object(
            status_processor,
            "get_campaign_event_publisher",
            new=AsyncMock(return_value=publisher),
        ),
        patch.object(status_processor, "_record_caller_id_outcome", outcome),
        patch.object(status_processor, "enqueue_job", new=AsyncMock()),
    ):
        yield SimpleNamespace(
            db=db,
            dispatcher=dispatcher,
            breaker=breaker,
            publisher=publisher,
            outcome=outcome,
            state=state,
        )


def _update(status, duration=None):
    return StatusCallbackRequest(call_id="call-1", status=status, duration=duration)


async def test_repeated_completed_applies_side_effects_once(processor_env):
    await _process_status_update(1, _update(TelephonyCallStatus.COMPLETED, "30"))
    await _process_status_update(1, _update(TelephonyCallStatus.COMPLETED, "30"))

    assert processor_env.breaker.record_and_evaluate.await_count == 1
    assert processor_env.outcome.await_count == 1
    assert processor_env.dispatcher.release_call_slot.await_count == 1
    # Both callbacks are still logged for audit.
    assert len(processor_env.state["logs"]["telephony_status_callbacks"]) == 2


async def test_late_busy_after_completed_does_not_overwrite(processor_env):
    await _process_status_update(1, _update(TelephonyCallStatus.COMPLETED, "30"))
    await _process_status_update(1, _update(TelephonyCallStatus.BUSY))

    processor_env.publisher.publish_retry_needed.assert_not_awaited()
    # No disposition overwrite: the only gathered_context write would come
    # from the not-connected branch.
    for call in processor_env.db.update_workflow_run.await_args_list:
        assert "gathered_context" not in call.kwargs


async def test_duplicate_no_answer_requests_one_retry(processor_env):
    await _process_status_update(1, _update(TelephonyCallStatus.NO_ANSWER))
    await _process_status_update(1, _update(TelephonyCallStatus.NO_ANSWER))
    assert processor_env.publisher.publish_retry_needed.await_count == 1
    assert processor_env.breaker.record_and_evaluate.await_count == 1


async def test_late_ringing_after_terminal_is_harmless(processor_env):
    await _process_status_update(1, _update(TelephonyCallStatus.BUSY))
    await _process_status_update(1, _update(TelephonyCallStatus.RINGING))
    assert processor_env.state["state"] == WorkflowRunState.COMPLETED.value
    assert processor_env.publisher.publish_retry_needed.await_count == 1


async def test_completed_after_busy_keeps_first_outcome(processor_env):
    await _process_status_update(1, _update(TelephonyCallStatus.BUSY))
    await _process_status_update(1, _update(TelephonyCallStatus.COMPLETED, "0"))
    assert processor_env.breaker.record_and_evaluate.await_count == 1
    assert processor_env.outcome.await_count == 1


async def test_late_completed_still_records_carrier_duration(processor_env):
    await _process_status_update(1, _update(TelephonyCallStatus.COMPLETED))
    await _process_status_update(1, _update(TelephonyCallStatus.COMPLETED, "44"))
    processor_env.db.record_telephony_duration.assert_awaited_with(1, 44)
