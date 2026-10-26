"""GENERATED — do not edit by hand.

Regenerate with `python -m dograh_sdk.codegen` against the target
Dograh backend. Source of truth: the backend's model-backed node-spec
catalog served from `/api/v1/node-types`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, Optional

from dograh_sdk.typed._base import TypedNode


@dataclass(kw_only=True)
class Hoppwhistle(TypedNode):
    """
    Insurance CRM, licensing and routing for AI callbacks and qualification
    LLM hint: Hoppwhistle is a post-call reporting integration for workflows
    that Hoppwhistle starts (AI Callback Recovery, AI Lead Qualifier). It
    sends the finished call's outcome, transcript, recording link, transfer
    attempts and usage back to Hoppwhistle, signed. It does not participate
    in the conversation graph and should not be connected to other nodes.
    The conversation's own tools are the Hoppwhistle tools installed by POST
    /api/v1/integrations/hoppwhistle/install.
    """

    type: ClassVar[str] = 'hoppwhistle'

    hoppwhistle_base_url: str
    """
    Base URL of the Hoppwhistle API, e.g. https://agents.example.com.
    """

    hoppwhistle_signing_secret: str
    """
    The shared secret (Hoppwhistle's DOGRAH_AI_AGENT_SECRET). Reports are
    signed with HMAC-SHA256; the secret itself is never sent.
    """

    name: str = 'Hoppwhistle'
    """
    Short identifier for this Hoppwhistle configuration.
    """

    hoppwhistle_enabled: bool = True
    """
    When false, finished calls are not reported to Hoppwhistle.
    """

    hoppwhistle_cost_per_minute_cents: Optional[float] = None
    """
    Optional: your all-in AI cost per minute, reported with each call so
    Hoppwhistle can show AI cost. Blank: Hoppwhistle estimates it.
    """

