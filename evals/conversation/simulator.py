"""Drive a workflow through a simulated call.

Runs the real engine — the same nodes, transitions, tools and extraction a
phone call uses — over the text-chat path, with a model playing the caller.
No audio, no telephony, no minutes billed, so a prompt change can be graded in
seconds instead of over days of live calls.
"""

from __future__ import annotations

import json
from typing import Any, Optional, Sequence
from uuid import uuid4

from loguru import logger

from api.db import db_client
from api.enums import WorkflowRunMode
from api.services.configuration.ai_model_configuration import (
    get_effective_ai_model_configuration_for_workflow,
)
from api.services.pipecat.service_factory import create_llm_service_from_provider
from api.services.workflow.run_creation import prepare_workflow_run_inputs
from api.services.workflow.text_chat_runner import (
    default_text_chat_checkpoint,
)
from api.services.workflow.text_chat_session_service import (
    append_text_chat_user_message,
    default_text_chat_session_data,
    execute_pending_text_chat_turn,
    initialize_text_chat_session,
    normalize_text_chat_session_data,
)
from evals.conversation.personas import (
    HANGUP_TOKEN,
    Persona,
    build_caller_prompt,
)
from evals.conversation.scoring import (
    ConversationResult,
    Turn,
    build_rubric_prompt,
    format_transcript,
    run_deterministic_checks,
)


async def _build_judge_llm(organization_id: int, workflow_configurations: dict):
    """An LLM for playing the caller and grading, from the org's own config."""
    config = await get_effective_ai_model_configuration_for_workflow(
        organization_id=organization_id,
        workflow_configurations=workflow_configurations or {},
    )
    if not config or not config.llm or not config.llm.api_key:
        return None
    return create_llm_service_from_provider(
        config.llm.provider, config.llm.model, config.llm.api_key
    )


async def _infer(llm, system_prompt: str, user_content: str) -> str:
    """One-shot completion. Imported lazily to keep pipecat off the CLI path."""
    from pipecat.processors.aggregators.llm_context import LLMContext

    context = LLMContext()
    context.set_messages([{"role": "user", "content": user_content}])
    return (await llm.run_inference(context, system_instruction=system_prompt)) or ""


def _latest_assistant_text(text_session) -> str:
    session_data = normalize_text_chat_session_data(text_session.session_data)
    turns = session_data.get("turns") or []
    if not turns:
        return ""
    message = turns[-1].get("assistant_message") or {}
    return str(message.get("text") or "")


def _is_finished(text_session) -> bool:
    session_data = normalize_text_chat_session_data(text_session.session_data)
    return str(session_data.get("status") or "") == "completed"


def parse_caller_reply(raw: str) -> tuple[str, bool]:
    """Split a caller model's output into speech and a hang-up signal.

    Models like to wrap replies in quotes or prefix them with a speaker label;
    both are stripped so the transcript reads like a call.
    """
    text = (raw or "").strip()
    hangup = HANGUP_TOKEN in text
    text = text.replace(HANGUP_TOKEN, "").strip()
    for prefix in ("caller:", "person:", "user:"):
        if text.lower().startswith(prefix):
            text = text[len(prefix) :].strip()
    text = text.strip('"').strip()
    return text, hangup


def parse_rubric_response(raw: str) -> tuple[Optional[float], str]:
    """Read ``{"score": n, "reason": "..."}`` out of a judge's reply."""
    if not raw:
        return None, ""
    try:
        start = raw.index("{")
        end = raw.rindex("}") + 1
        parsed = json.loads(raw[start:end])
    except (ValueError, json.JSONDecodeError):
        return None, ""
    if not isinstance(parsed, dict):
        return None, ""
    score = parsed.get("score")
    try:
        score = float(score)
    except (TypeError, ValueError):
        score = None
    return score, str(parsed.get("reason") or "")


async def simulate_conversation(
    *,
    workflow_id: int,
    organization_id: int,
    user_id: int,
    persona: Persona,
    conversion_dispositions: Sequence[str],
    judge: bool = True,
) -> ConversationResult:
    """Run one persona against one workflow and score the result."""
    result = ConversationResult(persona_key=persona.key)

    workflow = await db_client.get_workflow(
        workflow_id, organization_id=organization_id
    )
    if not workflow:
        result.error = f"Workflow {workflow_id} not found"
        return result

    caller_llm = await _build_judge_llm(
        organization_id, workflow.workflow_configurations or {}
    )
    if caller_llm is None:
        result.error = "No LLM configured for this organization"
        return result

    run_inputs = await prepare_workflow_run_inputs(
        db_client,
        workflow,
        initial_context=dict(persona.initial_context),
        use_draft=True,
        include_template_context=True,
    )
    workflow_run = await db_client.create_workflow_run(
        name=f"EVAL-{persona.key}-{uuid4().hex[:6].upper()}",
        workflow_id=workflow_id,
        mode=WorkflowRunMode.TEXTCHAT.value,
        user_id=user_id,
        initial_context=run_inputs.initial_context,
        organization_id=organization_id,
        definition_id=run_inputs.definition_id,
    )
    await db_client.update_workflow_run(
        workflow_run.id,
        annotations={"tester": {"source": "conversation_eval", "persona": persona.key}},
    )

    text_session = await db_client.ensure_workflow_run_text_session(
        workflow_run.id,
        session_data=default_text_chat_session_data(),
        checkpoint=default_text_chat_checkpoint(),
    )
    text_session = await initialize_text_chat_session(
        run_id=workflow_run.id, text_session=text_session
    )

    caller_prompt = build_caller_prompt(persona)

    try:
        text_session = await execute_pending_text_chat_turn(
            workflow_id=workflow_id,
            run_id=workflow_run.id,
            text_session=text_session,
        )
        opening = _latest_assistant_text(text_session)
        if opening:
            result.turns.append(Turn("agent", opening))

        for _ in range(persona.max_turns):
            if _is_finished(text_session):
                break

            reply_raw = await _infer(
                caller_llm,
                caller_prompt,
                f"The call so far:\n{format_transcript(result.turns)}\n\nYour reply:",
            )
            caller_text, hangup = parse_caller_reply(reply_raw)
            if caller_text:
                result.turns.append(Turn("caller", caller_text))
            if hangup or not caller_text:
                break

            text_session = await append_text_chat_user_message(
                run_id=workflow_run.id,
                text_session=text_session,
                user_text=caller_text,
                expected_revision=text_session.revision,
            )
            text_session = await execute_pending_text_chat_turn(
                workflow_id=workflow_id,
                run_id=workflow_run.id,
                text_session=text_session,
            )
            agent_text = _latest_assistant_text(text_session)
            if agent_text:
                result.turns.append(Turn("agent", agent_text))
    except Exception as e:
        logger.error(f"Simulated call for persona {persona.key} failed: {e}")
        result.error = str(e)

    refreshed = await db_client.get_workflow_run_by_id(workflow_run.id)
    gathered = (refreshed.gathered_context if refreshed else None) or {}
    result.disposition = str(gathered.get("mapped_call_disposition") or "")
    result.converted = result.disposition in conversion_dispositions
    result.node_path = [
        str(entry.get("node_name") or entry.get("node_id"))
        for entry in (gathered.get("node_path") or [])
        if isinstance(entry, dict)
    ]

    result.checks = run_deterministic_checks(
        result,
        forbidden=persona.must_not_say,
        should_convert=persona.should_convert,
        max_turns=persona.max_turns,
    )

    if judge and result.turns and not result.error:
        raw = await _infer(
            caller_llm,
            build_rubric_prompt(persona.description, persona.expected_outcome),
            f"The call:\n{format_transcript(result.turns)}",
        )
        result.rubric_score, result.rubric_reason = parse_rubric_response(raw)

    return result


async def simulate_all(
    *,
    workflow_id: int,
    organization_id: int,
    user_id: int,
    personas: Sequence[Persona],
    conversion_dispositions: Sequence[str],
    judge: bool = True,
) -> list[ConversationResult]:
    """Run every persona in turn.

    Sequential on purpose: these share an organization's model quota, and a
    burst of parallel calls trips rate limits far more often than it saves
    wall-clock on a handful of personas.
    """
    results: list[ConversationResult] = []
    for persona in personas:
        logger.info(f"Simulating persona '{persona.key}'")
        results.append(
            await simulate_conversation(
                workflow_id=workflow_id,
                organization_id=organization_id,
                user_id=user_id,
                persona=persona,
                conversion_dispositions=conversion_dispositions,
                judge=judge,
            )
        )
    return results


def format_scorecard(summary: dict[str, Any]) -> str:
    """Human-readable scorecard for a terminal or CI log."""
    lines = [
        "",
        f"Personas: {summary['personas']}  "
        f"Passed: {summary['passed']}  Failed: {summary['failed']}  "
        f"Pass rate: {summary['pass_rate']}%",
    ]
    if summary.get("avg_rubric_score") is not None:
        lines.append(f"Average rubric score: {summary['avg_rubric_score']}/10")
    lines.append("")

    for key, result in (summary.get("results") or {}).items():
        status = "PASS" if result["passed"] else "FAIL"
        score = result.get("rubric_score")
        score_text = f" score={score}" if score is not None else ""
        lines.append(f"  [{status}] {key}{score_text}")
        if result.get("error"):
            lines.append(f"         error: {result['error']}")
        for check in result.get("checks", []):
            if not check["passed"]:
                lines.append(f"         {check['name']}: {check['detail']}")
        if result.get("rubric_reason"):
            lines.append(f"         judge: {result['rubric_reason']}")
    lines.append("")
    return "\n".join(lines)
