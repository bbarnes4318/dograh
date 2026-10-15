"""A caller who speaks before the start-node greeting must not hear it twice.

With delayed_start and allow_interrupt, the caller's "Hello?" starts a live turn
that the start prompt answers. Queueing the static greeting on top of that made
the agent introduce itself twice back to back.
"""

from unittest.mock import AsyncMock, Mock

import pytest
from pipecat.frames.frames import TTSSpeakFrame
from pipecat.processors.aggregators.llm_context import LLMContext

from api.services.workflow.dto import (
    EdgeDataDTO,
    EndCallNodeData,
    Position,
    ReactFlowDTO,
    RFEdgeDTO,
    RFNodeDTO,
    StartCallNodeData,
)
from api.services.workflow.pipecat_engine import PipecatEngine
from api.services.workflow.workflow_graph import WorkflowGraph

GREETING = "Hi, it's Alex with the Final Expense Center."


@pytest.fixture
def workflow() -> WorkflowGraph:
    dto = ReactFlowDTO(
        nodes=[
            RFNodeDTO(
                id="start",
                type="startCall",
                position=Position(x=0, y=0),
                data=StartCallNodeData(
                    name="Start Call",
                    prompt="Start prompt",
                    is_start=True,
                    allow_interrupt=True,
                    add_global_prompt=False,
                    greeting=GREETING,
                    greeting_type="text",
                    extraction_enabled=False,
                ),
            ),
            RFNodeDTO(
                id="end",
                type="endCall",
                position=Position(x=0, y=200),
                data=EndCallNodeData(
                    name="End Call",
                    prompt="End prompt",
                    is_end=True,
                    allow_interrupt=False,
                    add_global_prompt=False,
                    extraction_enabled=False,
                ),
            ),
        ],
        edges=[
            RFEdgeDTO(
                id="start-end",
                source="start",
                target="end",
                data=EdgeDataDTO(label="End Call", condition="When the user is done"),
            ),
        ],
    )
    return WorkflowGraph(dto)


def _engine(workflow):
    llm = Mock()
    llm.queue_frame = AsyncMock()
    task = Mock()
    task.queue_frame = AsyncMock()
    engine = PipecatEngine(
        llm=llm,
        context=LLMContext(),
        workflow=workflow,
        call_context_vars={},
        workflow_run_id=1,
    )
    engine.set_task(task)
    return engine, task, llm


async def _open(engine, workflow):
    return await engine.queue_node_opening(
        node_id=workflow.start_node_id,
        previous_node_id=None,
        generate_if_no_greeting=True,
    )


@pytest.mark.asyncio
async def test_caller_turn_before_opening_skips_greeting(workflow):
    engine, task, llm = _engine(workflow)

    engine.mark_caller_turn_started()
    result = await _open(engine, workflow)

    assert result == "none"
    task.queue_frame.assert_not_awaited()
    llm.queue_frame.assert_not_awaited()


@pytest.mark.asyncio
async def test_silent_caller_still_gets_greeting(workflow):
    engine, task, llm = _engine(workflow)

    result = await _open(engine, workflow)

    assert result == "greeting"
    frame = task.queue_frame.await_args.args[0]
    assert isinstance(frame, TTSSpeakFrame)
    assert frame.text == GREETING


@pytest.mark.asyncio
async def test_caller_turn_after_greeting_queued_is_ignored(workflow):
    engine, task, _ = _engine(workflow)

    assert await _open(engine, workflow) == "greeting"
    engine.mark_caller_turn_started()

    # Entering the start node again later (e.g. a re-open) is unaffected: the
    # late turn was an ordinary interruption, not a pre-greeting hello.
    assert engine._caller_spoke_before_opening is False
    assert task.queue_frame.await_count == 1
