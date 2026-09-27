import asyncio
import json as _json
import re as _re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx as _httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from pydantic import BaseModel

from api.db import db_client
from api.db.models import UserModel
from api.services.auth.depends import get_user
from api.services.reports import ConversionReportService, DailyReportService
from api.services.reports.conversion_analytics import (
    COHORT_DIMENSIONS,
    DEFAULT_CONVERSION_DISPOSITIONS,
)
from api.services.campaign.rate_limiter import rate_limiter
from api.services.reports import DailyReportService
from api.services.storage import storage_fs

router = APIRouter(prefix="/organizations/reports")


class DailyReportResponse(BaseModel):
    date: str
    timezone: str
    workflow_id: Optional[int]
    metrics: Dict[str, int]
    disposition_distribution: List[Dict[str, Any]]
    call_duration_distribution: List[Dict[str, Any]]


class WorkflowOption(BaseModel):
    id: int
    name: str


class WorkflowRunDetail(BaseModel):
    phone_number: str
    disposition: str
    duration_seconds: float
    workflow_id: int
    run_id: int
    workflow_name: str
    created_at: str
    call_type: str


@router.get("/daily", response_model=DailyReportResponse)
async def get_daily_report(
    date: str = Query(..., description="Date in YYYY-MM-DD format"),
    timezone: str = Query(..., description="IANA timezone (e.g., 'America/New_York')"),
    workflow_id: Optional[int] = Query(
        None, description="Optional workflow ID to filter by"
    ),
    user: UserModel = Depends(get_user),
) -> DailyReportResponse:
    """
    Get daily report for the specified date and timezone.
    If workflow_id is provided, filters results to that specific workflow.
    If workflow_id is None, includes all workflows for the organization.
    """
    if not user.selected_organization_id:
        raise HTTPException(status_code=400, detail="No organization selected")

    # Validate date format
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(
            status_code=400, detail="Invalid date format. Use YYYY-MM-DD"
        )

    report_service = DailyReportService()

    try:
        report = await report_service.get_daily_report(
            organization_id=user.selected_organization_id,
            date=date,
            timezone=timezone,
            workflow_id=workflow_id,
        )
        return DailyReportResponse(**report)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/workflows", response_model=List[WorkflowOption])
async def get_workflow_options(
    user: UserModel = Depends(get_user),
) -> List[WorkflowOption]:
    """
    Get all workflows for the user's organization.
    Used to populate the workflow selector dropdown in the reports page.
    """
    if not user.selected_organization_id:
        raise HTTPException(status_code=400, detail="No organization selected")

    report_service = DailyReportService()

    workflows = await report_service.get_workflows_for_organization(
        organization_id=user.selected_organization_id
    )

    return [WorkflowOption(**w) for w in workflows]


@router.get("/daily/runs", response_model=List[WorkflowRunDetail])
async def get_daily_runs_detail(
    date: str = Query(..., description="Date in YYYY-MM-DD format"),
    timezone: str = Query(..., description="IANA timezone (e.g., 'America/New_York')"),
    workflow_id: Optional[int] = Query(
        None, description="Optional workflow ID to filter by"
    ),
    user: UserModel = Depends(get_user),
) -> List[WorkflowRunDetail]:
    """
    Get detailed workflow runs for the specified date.
    Used for CSV export functionality.
    """
    if not user.selected_organization_id:
        raise HTTPException(status_code=400, detail="No organization selected")

    # Validate date format
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(
            status_code=400, detail="Invalid date format. Use YYYY-MM-DD"
        )

    report_service = DailyReportService()

    try:
        runs = await report_service.get_daily_runs_detail(
            organization_id=user.selected_organization_id,
            date=date,
            timezone=timezone,
            workflow_id=workflow_id,
        )
        return [WorkflowRunDetail(**run) for run in runs]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class FunnelStep(BaseModel):
    node_id: str
    node_name: str
    reached: int
    reached_pct_of_runs: float
    dropped_from_previous: int
    drop_off_pct_from_previous: float
    converted: int
    conversion_pct_of_reached: float


class NodeFunnelResponse(BaseModel):
    start_date: str
    end_date: str
    timezone: str
    workflow_id: Optional[int]
    definition_id: Optional[int]
    total_runs: int
    runs_with_node_path: int
    conversion_dispositions: List[str]
    steps: List[FunnelStep]


class CohortRow(BaseModel):
    cohort: str
    runs: int
    converted: int
    conversion_pct: float
    avg_p95_latency_seconds: Optional[float]
    runs_with_latency: int


class ConversionCohortsResponse(BaseModel):
    start_date: str
    end_date: str
    timezone: str
    workflow_id: Optional[int]
    dimension: str
    conversion_dispositions: List[str]
    cohorts: List[CohortRow]


class TranscriptSearchResult(BaseModel):
    run_id: int
    run_name: str
    workflow_id: int
    workflow_name: str
    call_type: str
    created_at: Optional[str]
    excerpt: str


class TranscriptSearchResponse(BaseModel):
    query: str
    total: int
    limit: int
    offset: int
    results: List[TranscriptSearchResult]


def _require_organization(user: UserModel) -> int:
    if not user.selected_organization_id:
        raise HTTPException(status_code=400, detail="No organization selected")
    return user.selected_organization_id


def _validate_date_range(start_date: str, end_date: str) -> None:
    for value in (start_date, end_date):
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(
                status_code=400, detail="Invalid date format. Use YYYY-MM-DD"
            )
    if start_date > end_date:
        raise HTTPException(
            status_code=400, detail="start_date must not be after end_date"
        )


def _parse_conversion_dispositions(raw: Optional[str]) -> List[str]:
    if not raw:
        return list(DEFAULT_CONVERSION_DISPOSITIONS)
    codes = [code.strip() for code in raw.split(",") if code.strip()]
    if not codes:
        raise HTTPException(
            status_code=400, detail="conversion_dispositions must not be empty"
        )
    return codes


@router.get("/funnel", response_model=NodeFunnelResponse)
async def get_node_funnel(
    start_date: str = Query(..., description="Start date in YYYY-MM-DD format"),
    end_date: str = Query(..., description="End date in YYYY-MM-DD format"),
    timezone: str = Query("UTC", description="IANA timezone the dates are in"),
    workflow_id: Optional[int] = Query(None, description="Filter to one workflow"),
    definition_id: Optional[int] = Query(
        None, description="Filter to one workflow version"
    ),
    conversion_dispositions: Optional[str] = Query(
        None,
        description=(
            "Comma-separated disposition codes that count as a conversion. "
            "Defaults to XFER."
        ),
    ),
    user: UserModel = Depends(get_user),
) -> NodeFunnelResponse:
    """How far calls get through the workflow, and where they stop."""
    organization_id = _require_organization(user)
    _validate_date_range(start_date, end_date)

    try:
        funnel = await ConversionReportService().get_node_funnel(
            organization_id=organization_id,
            start_date=start_date,
            end_date=end_date,
            timezone=timezone,
            workflow_id=workflow_id,
            definition_id=definition_id,
            conversion_dispositions=_parse_conversion_dispositions(
                conversion_dispositions
            ),
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return NodeFunnelResponse(**funnel)


@router.get("/cohorts", response_model=ConversionCohortsResponse)
async def get_conversion_cohorts(
    start_date: str = Query(..., description="Start date in YYYY-MM-DD format"),
    end_date: str = Query(..., description="End date in YYYY-MM-DD format"),
    dimension: str = Query(..., description=f"One of: {', '.join(COHORT_DIMENSIONS)}"),
    timezone: str = Query("UTC", description="IANA timezone the dates are in"),
    workflow_id: Optional[int] = Query(None, description="Filter to one workflow"),
    conversion_dispositions: Optional[str] = Query(
        None,
        description=(
            "Comma-separated disposition codes that count as a conversion. "
            "Defaults to XFER."
        ),
    ),
    user: UserModel = Depends(get_user),
) -> ConversionCohortsResponse:
    """Conversion rate split by version, model, daypart and the like."""
    organization_id = _require_organization(user)
    _validate_date_range(start_date, end_date)

    if dimension not in COHORT_DIMENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"dimension must be one of: {', '.join(COHORT_DIMENSIONS)}",
        )

    try:
        cohorts = await ConversionReportService().get_conversion_cohorts(
            organization_id=organization_id,
            start_date=start_date,
            end_date=end_date,
            timezone=timezone,
            dimension=dimension,
            workflow_id=workflow_id,
            conversion_dispositions=_parse_conversion_dispositions(
                conversion_dispositions
            ),
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return ConversionCohortsResponse(**cohorts)


@router.get("/transcripts/search", response_model=TranscriptSearchResponse)
async def search_transcripts(
    q: str = Query(..., min_length=2, description="Search text, e.g. 'too expensive'"),
    workflow_id: Optional[int] = Query(None, description="Filter to one workflow"),
    start_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    timezone: str = Query("UTC", description="IANA timezone the dates are in"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: UserModel = Depends(get_user),
) -> TranscriptSearchResponse:
    """Find calls by what was said on them."""
    organization_id = _require_organization(user)
    if (start_date is None) != (end_date is None):
        raise HTTPException(
            status_code=400, detail="Provide both start_date and end_date, or neither"
        )
    if start_date and end_date:
        _validate_date_range(start_date, end_date)

    try:
        found = await ConversionReportService().search_transcripts(
            organization_id=organization_id,
            query_text=q,
            workflow_id=workflow_id,
            start_date=start_date,
            end_date=end_date,
            timezone=timezone,
            limit=limit,
            offset=offset,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return TranscriptSearchResponse(**found)
# ---------------------------------------------------------------------------
# Campaign Insights (Hopwhistle) — live funnel + per-call human breakdown.
# Classifies each completed call's transcript (human / voicemail / echo /
# no-speech), derives an outcome + non-conversion reason, and caches the
# result per run in Redis so live polling stays cheap.
# ---------------------------------------------------------------------------

_CLS_CACHE_PREFIX = "insights:v3:"
_CLS_CACHE_TTL = 60 * 60 * 24 * 30  # 30 days

_VM_PHRASES = [
    "leave a message", "leave your message", "please leave", "at the tone",
    "after the tone", "after the beep", "record your message", "please record",
    "voicemail", "voice mail", "voice message", "not available", "unavailable",
    "google voice", "google subscriber", "mailbox", "press 1", "press one",
    "the person you are trying to reach", "cannot take your call",
    "automated voice", "has been forwarded", "please try your call again",
    "is not able to take", "you have reached", "cannot receive messages",
    "i'll see if this person is available", "record your name",
]
_XFER_PHRASES = [
    "bring the licensed agent onto the line",
    "get a licensed agent on the line",
    "licensed agent onto the line",
]
_DNC_PHRASES = [
    "take me off", "do not call", "don't call", "remove me", "stop calling",
    "off your list", "off your call", "quit calling", "leave me alone",
]
_NOT_INTERESTED_PHRASES = [
    "not interested", "don't want", "do not want", "already have",
    "i have my own", "no thank", "not looking", "already got",
    "already covered", "everything's already paid",
]
_AUDIO_PHRASES = [
    "can't hear", "cannot hear", "hardly hear", "can you hear me",
    "i can't hear you", "sound the same", "you're breaking up", "bad connection",
]
_BUSY_PHRASES = ["at work right now", "i'm at work", "busy right now", "in the middle of"]


def _parse_turns(txt: str):
    out = []
    for line in txt.splitlines():
        m = _re.match(r"\[([^\]]+)\]\s*(assistant|user):\s*(.*)", line.strip())
        if m:
            out.append((m.group(1), m.group(2), m.group(3)))
    return out


_MAX_LLM_PER_REQ = 80  # bound LLM judgments per request (post-backfill rarely hit)
_LLM_CFG_CACHE: Dict[int, Optional[Dict[str, str]]] = {}

_LLM_SYS = (
    "You label outbound phone-call transcripts. 'assistant' is our AI agent; "
    "'user' is whatever answered the phone. Decide what the USER side actually is:\n"
    "voicemail = a recorded answering-machine / voicemail greeting or carrier system "
    "(e.g. 'leave your name and number', \"you've reached\", 'sorry I missed your call', "
    "'at the beep', \"I'll get back to you\", 'press pound', 'mailbox', a name followed by "
    "'leave a message'). Answer voicemail even if our AI kept talking as if it were a live person.\n"
    "human = a live person actually reacting (yes/no answers, 'who is this', objecting, "
    "cursing, declining, small talk, asking questions).\n"
    "silence = user said nothing meaningful / only noise.\n"
    "Reply with ONLY one word: voicemail, human, or silence."
)


async def _get_llm_cfg(org_id: int) -> Optional[Dict[str, str]]:
    """Read the org's configured LLM (OpenAI-compatible, e.g. Grok) creds for classification."""
    if org_id in _LLM_CFG_CACHE:
        return _LLM_CFG_CACHE[org_id]
    cfg = None
    try:
        from sqlalchemy import text as _t
        async with db_client.async_session() as session:
            row = (await session.execute(_t(
                "select value from organization_configurations "
                "where organization_id=:o and key='MODEL_CONFIGURATION_V2' limit 1"
            ), {"o": org_id})).mappings().first()
        val = row["value"] if row else None
        if isinstance(val, str):
            val = _json.loads(val)
        llm = val["byok"]["pipeline"]["llm"]
        key = llm["api_key"]
        if isinstance(key, list):
            key = key[0]
        cfg = {"key": key, "base_url": llm["base_url"], "model": llm["model"]}
    except Exception:
        cfg = None
    _LLM_CFG_CACHE[org_id] = cfg
    return cfg


def _needs_llm(txt: str) -> bool:
    """Only ambiguous calls (both sides spoke) need an LLM human/voicemail judgment."""
    return bool(txt) and "] user:" in txt and "] assistant:" in txt


async def _llm_category(txt: str, client, cfg: Dict[str, str], sem: asyncio.Semaphore) -> Optional[str]:
    if not cfg or not txt:
        return None
    async with sem:
        try:
            r = await client.post(
                cfg["base_url"].rstrip("/") + "/chat/completions",
                headers={"Authorization": "Bearer " + cfg["key"]},
                json={
                    "model": cfg["model"],
                    "messages": [
                        {"role": "system", "content": _LLM_SYS},
                        {"role": "user", "content": "Transcript:\n" + txt[:2500]},
                    ],
                    "temperature": 0,
                    "max_tokens": 4,
                },
                timeout=25,
            )
            if r.status_code != 200:
                return None
            out = (r.json()["choices"][0]["message"]["content"] or "").strip().lower()
        except Exception:
            return None
        if "voicemail" in out or "machine" in out:
            return "voicemail"
        if "human" in out:
            return "human"
        if "silence" in out or "silent" in out or "no_speech" in out:
            return "no_speech"
        return None


def _classify_run(txt: str, dispo: Optional[str], nodes_visited: List[str], transfer_state: Optional[str], category_override: Optional[str] = None) -> Dict[str, Any]:
    """Classify one call transcript. Returns a JSON-serializable summary dict."""
    turns = _parse_turns(txt or "")
    users = [t[2].strip() for t in turns if t[1] == "user" and t[2].strip()]
    asst = [t[2].strip() for t in turns if t[1] == "assistant"]
    low_all_users = " ".join(u.lower() for u in users)
    low_full = (txt or "").lower()

    # duration from transcript timestamps
    dur = 0
    try:
        ts = [datetime.fromisoformat(t[0]) for t in turns]
        if len(ts) >= 2:
            dur = int((max(ts) - min(ts)).total_seconds())
    except Exception:
        dur = 0

    # category — VM phrases are high precision (win); else trust the LLM judgment; else heuristic
    if not turns or not asst:
        category = "no_answer"
    elif any(p in low_all_users for p in _VM_PHRASES):
        category = "voicemail"
    elif category_override in ("human", "voicemail", "no_speech", "echo"):
        category = category_override
    elif not users:
        category = "no_speech"
    else:
        low_asst_joined = " ".join(a.lower() for a in asst)
        echoes = sum(1 for u in users if u.lower() in low_asst_joined)
        if len(users) >= 3 and echoes >= max(2, len(users) * 0.6):
            category = "echo"
        else:
            category = "human"

    reached_offer = "Offer Transfer" in (nodes_visited or []) or (
        "bring a licensed agent on" in low_full or "is it okay if i bring" in low_full
    )
    fired_transfer = dispo == "transfer_call" or any(p in low_full for p in _XFER_PHRASES)
    transfer_answered = transfer_state in ("terminated", "completed")

    dnc = any(k in low_all_users for k in _DNC_PHRASES)
    not_interested = any(k in low_all_users for k in _NOT_INTERESTED_PHRASES)
    audio_issue = any(k in low_full for k in _AUDIO_PHRASES) or (
        sum(1 for u in users if u.lower().strip(" .?!") == "hello") >= 2
    )
    busy = any(k in low_all_users for k in _BUSY_PHRASES)

    # outcome (mirrors the verified analysis rules)
    if fired_transfer:
        result = "Transferred - buyer answered" if transfer_answered else "Transferred - buyer no-answer"
    elif dnc:
        result = "DNC / remove request"
    elif reached_offer and category == "human":
        result = "Offer made - declined"
    elif dispo == "user_qualified":
        result = "Qualified (pre-offer)"
    elif not_interested:
        result = "Not interested"
    elif dispo == "user_idle_max_duration_exceeded":
        result = "Went silent / dead air"
    elif dispo == "user_hangup":
        result = "Hung up during convo"
    elif dispo == "end_call_tool":
        result = "Ended by agent"
    elif dispo == "pipeline_error":
        result = "System error"
    else:
        result = dispo or "No answer"

    # non-conversion reason (humans only)
    reason = None
    if category == "human" and not transfer_answered:
        if fired_transfer:
            reason = "Interested - lost at handoff (buyer no-answer)"
        elif dnc:
            reason = "Asked to be removed (DNC)"
        elif not_interested:
            reason = "Not interested / already covered"
        elif reached_offer:
            reason = "Declined at the agent offer"
        elif audio_issue:
            reason = "Audio / connection problems"
        elif busy:
            reason = "Busy - bad timing"
        elif dispo == "user_idle_max_duration_exceeded":
            reason = "Went silent mid-call"
        elif dispo == "user_hangup" and dur <= 20:
            reason = "Hung up during the opener (first 20s)"
        elif dispo == "user_hangup":
            reason = "Hung up mid-conversation"
        else:
            reason = "Conversation fizzled out"

    voicemail_leak = category == "voicemail" and len(asst) >= 3
    warm_drop = (
        category == "human"
        and not fired_transfer
        and not dnc
        and not not_interested
        and dur >= 30
        and dispo in ("user_hangup", "user_idle_max_duration_exceeded")
    )

    return {
        "category": category,
        "result": result,
        "reason": reason,
        "duration": dur,
        "transcript": txt or "",
        "reached_offer": bool(reached_offer),
        "fired_transfer": bool(fired_transfer),
        "transfer_answered": bool(transfer_answered),
        "audio_issue": bool(audio_issue),
        "opener_drop": bool(category == "human" and dispo == "user_hangup" and dur <= 20),
        "voicemail_leak": bool(voicemail_leak),
        "warm_drop": bool(warm_drop),
        "turns_user": len(users),
    }


async def _fetch_transcript(run_id: int, sem: asyncio.Semaphore, client: _httpx.AsyncClient):
    async with sem:
        try:
            def _read_minio():
                res = storage_fs.client.get_object(storage_fs.bucket_name, f"transcripts/{run_id}.txt")
                return res.read().decode('utf-8')
            txt = await asyncio.to_thread(_read_minio)
            return run_id, txt
        except Exception:
            return run_id, ""


@router.get("/campaign-insights")
async def get_campaign_insights(
    date: Optional[str] = Query(None, description="Single date YYYY-MM-DD (legacy; use start_date/end_date for a range)"),
    start_date: Optional[str] = Query(None, description="Range start YYYY-MM-DD (inclusive)"),
    end_date: Optional[str] = Query(None, description="Range end YYYY-MM-DD (inclusive)"),
    timezone: str = Query("America/New_York", description="IANA timezone"),
    workflow_id: Optional[int] = Query(None),
    user: UserModel = Depends(get_user),
) -> Dict[str, Any]:
    """Live campaign funnel, outcome breakdown, non-conversion reasons,
    coaching insights, and a per-call table for a local date or date range."""
    if not user.selected_organization_id:
        raise HTTPException(status_code=400, detail="No organization selected")
    s_str = start_date or date
    e_str = end_date or date
    if not s_str or not e_str:
        raise HTTPException(status_code=400, detail="Provide date, or both start_date and end_date")
    try:
        s_day = datetime.strptime(s_str, "%Y-%m-%d")
        e_day = datetime.strptime(e_str, "%Y-%m-%d")
        tz = ZoneInfo(timezone)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid date or timezone")
    if e_day < s_day:
        s_day, e_day = e_day, s_day
    if (e_day - s_day).days > 186:
        raise HTTPException(status_code=400, detail="Date range too large (max 186 days)")

    start_utc = s_day.replace(tzinfo=tz).astimezone(ZoneInfo("UTC"))
    end_utc = (e_day.replace(tzinfo=tz) + timedelta(days=1)).astimezone(ZoneInfo("UTC"))

    org_id = user.selected_organization_id

    # ---- pull the day's runs (raw SQL via the async engine already configured)
    from sqlalchemy import text as _sql_text

    async with db_client.async_session() as session:
        q = """
            select r.id, r.state, r.is_completed, r.created_at,
                   COALESCE(NULLIF(r.initial_context->>'phone_number',''), r.initial_context->>'called_number') as phone,
                   r.call_type as direction,
                   r.gathered_context->>'call_disposition' as dispo,
                   r.gathered_context->>'transfer_state' as transfer_state,
                   r.gathered_context->'nodes_visited' as nodes_visited,
                   r.campaign_id,
                   r.recording_url as recording_url,
                   r.transcript_url as transcript_url,
                   r.usage_info->>'call_duration_seconds' as stored_duration
            from workflow_runs r
            join workflows w on w.id = r.workflow_id
            where w.organization_id = :org
              and r.created_at >= :start and r.created_at < :end
              and r.mode != 'chat'
        """
        params: Dict[str, Any] = {"org": org_id, "start": start_utc, "end": end_utc}
        if workflow_id:
            q += " and r.workflow_id = :wf"
            params["wf"] = workflow_id
        q += " order by r.created_at"
        rows = (await session.execute(_sql_text(q), params)).mappings().all()

    total = len(rows)
    active = sum(1 for r in rows if r["state"] == "running")
    completed_rows = [r for r in rows if r["state"] not in ("running", "queued")]

    # ---- classification with Redis cache
    redis = await rate_limiter._get_redis()
    run_ids = [r["id"] for r in completed_rows]
    cached: Dict[int, Dict[str, Any]] = {}
    if run_ids:
        keys = [f"{_CLS_CACHE_PREFIX}{rid}" for rid in run_ids]
        vals = await redis.mget(keys)
        for rid, v in zip(run_ids, vals):
            if v:
                try:
                    cached[rid] = _json.loads(v)
                except Exception:
                    pass

    to_classify = [r for r in completed_rows if r["id"] not in cached]
    if to_classify:
        sem = asyncio.Semaphore(40)
        cats: Dict[int, Optional[str]] = {}
        async with _httpx.AsyncClient(timeout=30) as client:
            texts = dict(
                await asyncio.gather(
                    *[_fetch_transcript(r["id"], sem, client) for r in to_classify]
                )
            )
            llm_cfg = await _get_llm_cfg(org_id)
            if llm_cfg:
                targets = [r for r in to_classify if _needs_llm(texts.get(r["id"], ""))][:_MAX_LLM_PER_REQ]
                if targets:
                    llm_sem = asyncio.Semaphore(8)
                    res = await asyncio.gather(
                        *[_llm_category(texts.get(r["id"], ""), client, llm_cfg, llm_sem) for r in targets]
                    )
                    cats = {r["id"]: c for r, c in zip(targets, res)}
        pipe = redis.pipeline()
        for r in to_classify:
            nodes = r["nodes_visited"]
            if isinstance(nodes, str):
                try:
                    nodes = _json.loads(nodes)
                except Exception:
                    nodes = []
            cls = _classify_run(texts.get(r["id"], ""), r["dispo"], nodes or [], r["transfer_state"], category_override=cats.get(r["id"]))
            cached[r["id"]] = cls
            pipe.set(f"{_CLS_CACHE_PREFIX}{r['id']}", _json.dumps(cls), ex=_CLS_CACHE_TTL)
        try:
            await pipe.execute()
        except Exception as e:
            logger.warning(f"insights cache write failed: {e}")

    # ---- real call duration
    # The classification works duration out from transcript timestamps,
    # which is 0 for any transcript with fewer than two of them and is
    # cached in Redis. The platform stores the actual call length, so it
    # wins whenever it is present; the transcript span stays the fallback.
    for r in completed_rows:
        c = cached.get(r["id"])
        if not c:
            continue
        try:
            stored = float(r["stored_duration"] or 0)
        except (TypeError, ValueError):
            stored = 0
        if stored > 0:
            cached[r["id"]] = {**c, "duration": int(round(stored))}

    # ---- aggregate
    def _cnt(pred) -> int:
        return sum(1 for r in completed_rows if r["id"] in cached and pred(cached[r["id"]]))

    connected = _cnt(lambda c: c["category"] != "no_answer")
    humans = _cnt(lambda c: c["category"] == "human")
    voicemails = _cnt(lambda c: c["category"] in ("voicemail", "echo"))
    offers = _cnt(lambda c: c["category"] == "human" and c["reached_offer"])
    transfers = _cnt(lambda c: c["fired_transfer"])
    transfers_answered = _cnt(lambda c: c["transfer_answered"])

    outcome_counts: Dict[str, int] = {}
    reason_counts: Dict[str, int] = {}
    talk_seconds = 0
    for r in completed_rows:
        c = cached.get(r["id"])
        if not c:
            continue
        talk_seconds += c["duration"]
        if c["category"] == "human":
            outcome_counts[c["result"]] = outcome_counts.get(c["result"], 0) + 1
            if c["reason"]:
                reason_counts[c["reason"]] = reason_counts.get(c["reason"], 0) + 1

    opener_drops = _cnt(lambda c: c["opener_drop"])
    audio_losses = _cnt(lambda c: c["category"] == "human" and c["audio_issue"])
    vm_leaks = _cnt(lambda c: c["voicemail_leak"])
    warm_drops = _cnt(lambda c: c["warm_drop"])
    silent = _cnt(lambda c: c["category"] == "human" and c["result"] == "Went silent / dead air")

    insights: List[Dict[str, Any]] = []
    if transfers > 0 and transfers_answered == 0:
        insights.append({
            "severity": "critical",
            "title": "Buyers are not answering transfers",
            "detail": f"{transfers} interested prospect(s) said YES and were transferred, but 0 were picked up by a buyer. Every transfer is currently lost at the handoff. Staff the buyer lines before the next run.",
            "count": transfers,
        })
    elif transfers > 0 and transfers_answered / transfers < 0.5:
        insights.append({
            "severity": "critical",
            "title": "Most transfers are going unanswered",
            "detail": f"Only {transfers_answered} of {transfers} transfers were picked up by a buyer. Interested prospects are being lost at the handoff - check buyer line staffing.",
            "count": transfers - transfers_answered,
        })
    elif transfers > 0:
        insights.append({
            "severity": "good",
            "title": "Transfers are connecting",
            "detail": f"{transfers_answered} of {transfers} transfers were answered by a buyer.",
            "count": transfers_answered,
        })
    if opener_drops:
        insights.append({
            "severity": "warn",
            "title": "Hang-ups during the opener",
            "detail": f"{opener_drops} human(s) hung up in the first 20 seconds. The conversational opener (workflow v4+) targets exactly this - watch whether this number falls on the next run.",
            "count": opener_drops,
        })
    if audio_losses:
        insights.append({
            "severity": "warn",
            "title": "Audio / connection losses",
            "detail": f"{audio_losses} human call(s) showed \"can't hear you\" or hello-loops. The instant greeting (v4) removes pickup dead-air; remaining cases point at line quality.",
            "count": audio_losses,
        })
    if warm_drops:
        insights.append({
            "severity": "info",
            "title": "Warm prospects worth a re-dial",
            "detail": f"{warm_drops} engaged human(s) (30s+ of real conversation, no refusal) dropped mid-call. These are recoverable leads.",
            "count": warm_drops,
        })
    if vm_leaks:
        insights.append({
            "severity": "info",
            "title": "Talking to answering machines",
            "detail": f"Alex kept talking on {vm_leaks} voicemail(s)/screening bot(s). Tightening machine detection saves talk-time and caller-ID reputation.",
            "count": vm_leaks,
        })
    if silent:
        insights.append({
            "severity": "info",
            "title": "Dead-air endings",
            "detail": f"{silent} human call(s) ended in silence (prospect stopped responding).",
            "count": silent,
        })

    # ---- per-call rows
    calls: List[Dict[str, Any]] = []
    for r in completed_rows:
        c = cached.get(r["id"])
        if not c:
            continue
        txt = c.get("transcript") or ""
        if not txt and r.get("transcript_url"):
            try:
                obj = storage_fs.client.get_object(storage_fs.bucket_name, f"transcripts/{r['id']}.txt")
                txt = obj.read().decode("utf-8")
            except Exception:
                txt = ""
        calls.append({
            "run_id": r["id"],
            "time": r["created_at"].astimezone(tz).isoformat(),
            "phone": r["phone"],
            "direction": (r["direction"] or "").capitalize(),
            "duration": c["duration"],
            "category": c["category"],
            "result": c["result"],
            "reason": c["reason"],
            "recording_url": r["recording_url"],
            "transcript_url": r["transcript_url"],
            "transcript": txt,
            "transfer": bool(c["fired_transfer"]),
        })

    last_call_at = (
        max(r["created_at"] for r in rows).astimezone(tz).isoformat() if rows else None
    )

    return {
        "date": e_str,
        "start_date": s_str,
        "end_date": e_str,
        "timezone": timezone,
        "generated_at": datetime.now(ZoneInfo("UTC")).isoformat(),
        "status": {
            "active_calls": active,
            "total_dials": total,
            "last_call_at": last_call_at,
        },
        "funnel": [
            {"stage": "Dials", "count": total},
            {"stage": "Answered", "count": connected},
            {"stage": "Humans reached", "count": humans},
            {"stage": "Reached the offer", "count": offers},
            {"stage": "Interested (transferred)", "count": transfers},
            {"stage": "Buyer answered", "count": transfers_answered},
        ],
        "kpis": {
            "dials": total,
            "answered": connected,
            "voicemails": voicemails,
            "humans": humans,
            "offers": offers,
            "transfers": transfers,
            "transfers_answered": transfers_answered,
            "talk_minutes": round(talk_seconds / 60),
            "human_rate": round(humans / connected * 100, 1) if connected else 0.0,
            "offer_rate": round(offers / humans * 100, 1) if humans else 0.0,
            "transfer_rate": round(transfers / humans * 100, 1) if humans else 0.0,
        },
        "outcomes": [
            {"result": k, "count": v}
            for k, v in sorted(outcome_counts.items(), key=lambda kv: -kv[1])
        ],
        "reasons": [
            {"reason": k, "count": v}
            for k, v in sorted(reason_counts.items(), key=lambda kv: -kv[1])
        ],
        "insights": insights,
        "calls": calls,
    }
