"""Hoppwhistle Recruiting integration package.

Hoppwhistle's AI Recruiting Studio runs candidate-requested AI phone
interviews on this engine. Hoppwhistle owns the candidate, consent, the
interview framework, the approved agency facts, evidence and decisions;
Dograh owns the conversation. Recruiting is deliberately separate from the
``hoppwhistle`` (sales) package: different tools, a different secret, a
different report endpoint, so a candidate call can never touch insurance
sales data.

Provides:
- ``HoppwhistleRecruitingNodeData`` -- the reporting node a recruiting
  workflow carries; finished runs Hoppwhistle started are reported, signed
  (completion.py)
- ``POST /api/v1/integrations/hoppwhistle-recruiting/install`` -- install or
  refresh the recruiting tools (tool_catalog.py) in the caller's organization
- ``POST /api/v1/integrations/hoppwhistle-recruiting/workflows`` -- create or
  update, then publish, a workflow Hoppwhistle compiled from an interview
  framework (tool names resolved to this organization's tools)
- ``GET  /api/v1/integrations/hoppwhistle-recruiting/workflows/{uuid}/inspect``
  -- the PUBLISHED workflow as plain facts, which Hoppwhistle compares with
  what it compiled before every call (its protected-control check)
- ``GET  /api/v1/integrations/hoppwhistle-recruiting/runs?session_id=`` -- runs
  started for a Hoppwhistle session (dispatch reconciliation)

The contract: docs/integrations/hoppwhistle-recruiting.mdx here, and
docs/recruiting/AI_VOICE_CONTRACT.md in Hoppwhistle.
"""

from __future__ import annotations

from dataclasses import fields as _fields

from api.services.integrations.base import IntegrationPackageSpec
from api.services.integrations.registry import register_package

from .completion import run_completion
from .node import NODE
from .protected import validate_recruiting_definition
from .routes import router

_spec_kwargs = dict(
    name="hoppwhistle_recruiting",
    nodes=(NODE,),
    routers=(router,),
    run_completion=run_completion,
)
# The publish-gate hook exists in this fork's framework; an engine running an
# older image (the add-on mount in production) loads the package without it,
# and Hoppwhistle's dial-time conformance check still refuses edited workflows.
if "validate_definition" in {f.name for f in _fields(IntegrationPackageSpec)}:
    _spec_kwargs["validate_definition"] = validate_recruiting_definition

PACKAGE = register_package(IntegrationPackageSpec(**_spec_kwargs))

__all__ = ["PACKAGE"]
