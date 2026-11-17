"""Holiday Voices integration package.

Holiday Voices is a retail store (recordable keepsake ornaments) run on
Hoppwhistle's retail workspace, with its OWN organization here. Dograh holds
the conversation; Hoppwhistle holds the catalog, customers, orders, checkout
hand-off (WooCommerce is the payment authority) and support.

Provides:
- ``HolidayVoicesNodeData`` -- configuration node; a workflow carrying it
  reports each finished store call back to Hoppwhistle (completion.py)
- ``run_completion`` -- that post-call report
- ``POST /api/v1/integrations/holiday-voices/install`` and ``cli.py`` --
  idempotent install of the engine-key credential, the commerce tools
  (tool_catalog.py) and the four agents (templates.py)

The contract: docs/integrations/holiday-voices.mdx here, and
docs/holiday-voices/AI_VOICE_SPECIFICATION.md in Hoppwhistle.
"""

from __future__ import annotations

from api.services.integrations.base import IntegrationPackageSpec
from api.services.integrations.registry import register_package

from .completion import run_completion
from .node import NODE
from .routes import router

PACKAGE = register_package(
    IntegrationPackageSpec(
        name="holiday_voices",
        nodes=(NODE,),
        routers=(router,),
        run_completion=run_completion,
    )
)

__all__ = ["PACKAGE"]
