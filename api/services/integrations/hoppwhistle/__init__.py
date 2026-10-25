"""Hoppwhistle integration package.

Hoppwhistle is the insurance operating platform (CRM, consent, licensing,
routing, telephony) behind the AI Callback Recovery and AI Lead Qualifier
products. Dograh holds the conversation; Hoppwhistle holds everything else.

Provides:
- ``HoppwhistleNodeData`` -- a configuration node; a workflow carrying it
  reports each finished Hoppwhistle-started call back, signed (completion.py)
- ``run_completion`` -- that post-call report
- ``POST /api/v1/integrations/hoppwhistle/install`` -- installs the
  Hoppwhistle tools (tool_catalog.py) and, optionally, starter workflows
  (templates.py) into an organization

The contract: docs/integrations/hoppwhistle.mdx here, and
docs/ai-agents/DOGRAH_CONTRACT.md in Hoppwhistle.
"""

from __future__ import annotations

from api.services.integrations.base import IntegrationPackageSpec
from api.services.integrations.registry import register_package

from .completion import run_completion
from .node import NODE
from .routes import router

PACKAGE = register_package(
    IntegrationPackageSpec(
        name="hoppwhistle",
        nodes=(NODE,),
        routers=(router,),
        run_completion=run_completion,
    )
)

__all__ = ["PACKAGE"]
