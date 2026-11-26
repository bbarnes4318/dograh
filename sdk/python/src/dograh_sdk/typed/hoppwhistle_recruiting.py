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
class Hoppwhistle_recruiting(TypedNode):
    """
    Reports AI phone interviews to Hoppwhistle's AI Recruiting Studio  LLM
    hint: Hoppwhistle Recruiting is a post-call reporting node for AI phone
    interviews that Hoppwhistle compiles and starts. It sends the finished
    call's transcript, recording link, steps visited, extracted variables
    and usage back to Hoppwhistle, signed. It does not take part in the
    conversation and must not be connected to other nodes. The workflow's
    protected steps are checked by Hoppwhistle before every call; edit
    recruiting workflows from the Studio, not here.
    """

    type: ClassVar[str] = 'hoppwhistle_recruiting'

    recruiting_base_url: str
    """
    Base URL of the Hoppwhistle API.
    """

    recruiting_signing_secret: str
    """
    Hoppwhistle's RECRUITING_VOICE_ENGINE_SECRET. Reports are HMAC-signed;
    the secret is never sent.
    """

    name: str = 'Hoppwhistle Recruiting'
    """
    Short identifier shown in the canvas and call logs.
    """

    recruiting_enabled: bool = True
    """
    When false, finished interviews are not reported (and Hoppwhistle will
    refuse to call).
    """

    recruiting_contract_version: Optional[str] = None
    """
    Set by Hoppwhistle when it publishes the workflow.
    """

    recruiting_framework_version: Optional[str] = None
    """
    Set by Hoppwhistle when it publishes the workflow.
    """

