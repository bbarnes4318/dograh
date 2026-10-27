"""Which inbound calls Dograh may answer.

Hopwhistle owns inbound compliance and routing. It screens every external
caller against the TCPA litigator list before it routes the call anywhere, and
only a caller it has approved is handed to Dograh -- over the
``hopwhistle-callback`` PJSIP endpoint that Hopwhistle's
``deploy/dograh/inbound-callback`` installs in Asterisk (it matches only
FreeSWITCH's address).

Dograh does not screen callers itself and must not try to. What it can do is
refuse any inbound call that did not come from Hopwhistle, because such a call
was never screened:

- an ARI call that entered Stasis from any other endpoint -- for example a
  carrier trunk whose Asterisk context hands calls straight to the Stasis app;
- a provider webhook (Twilio, Vonage, Telnyx, ...) starting an inbound run
  directly from the PSTN.

Whether it does is up to each organization: Platform Settings -> "Answer only
callers screened by Hopwhistle" (the ``inbound_screening_required``
organization preference). It is OFF by default, and then every inbound call is
answered as before. When it is ON:

``ARI_INBOUND_ALLOWED_ENDPOINTS``
    Comma-separated PJSIP endpoint names an inbound ARI call may arrive on.
    Default ``hopwhistle-callback``. ``*`` admits every endpoint, which
    re-opens the unscreened path; only do that where no endpoint can carry
    external callers.

``DIRECT_CARRIER_INBOUND_ENABLED``
    ``true`` lets provider webhooks (``/inbound/run``, ``/inbound/{id}``)
    start inbound runs anyway. Default ``false``: those calls never passed
    through Hopwhistle.
"""

import os
import re
from typing import Optional

from loguru import logger

from api.services.organization_preferences import inbound_screening_required

DEFAULT_ARI_INBOUND_ENDPOINTS = "hopwhistle-callback"

# ``PJSIP/hopwhistle-callback-0000002a`` -> ``hopwhistle-callback``. Asterisk
# appends a hex sequence number to every channel of an endpoint.
_CHANNEL_NAME = re.compile(r"^[A-Za-z0-9_]+/(?P<endpoint>.+)-[0-9a-fA-F]+$")


async def screening_required(organization_id: Optional[int]) -> bool:
    """Whether this organization answers only Hopwhistle-screened callers.

    If the setting can't be read, assume it is on: a call Hopwhistle handed
    over is still answered, and an unscreened one is never let through by an
    outage.
    """
    try:
        return await inbound_screening_required(organization_id)
    except Exception as e:
        logger.warning(
            f"Could not read inbound screening setting for org "
            f"{organization_id}; treating it as on: {e}"
        )
        return True


def channel_endpoint(channel_name: Optional[str]) -> Optional[str]:
    """The endpoint a channel belongs to, from its name, or None."""
    match = _CHANNEL_NAME.match(channel_name or "")
    return match.group("endpoint") if match else None


def allowed_ari_inbound_endpoints(env: Optional[dict] = None) -> set[str]:
    source = os.environ if env is None else env
    raw = source.get("ARI_INBOUND_ALLOWED_ENDPOINTS")
    if raw is None:
        raw = DEFAULT_ARI_INBOUND_ENDPOINTS
    return {name.strip() for name in raw.split(",") if name.strip()}


def ari_inbound_admitted(
    channel_name: Optional[str],
    *,
    screening_required: bool,
    env: Optional[dict] = None,
) -> bool:
    """Whether to answer an inbound ARI call that arrived on this channel."""
    if not screening_required:
        return True
    allowed = allowed_ari_inbound_endpoints(env)
    if "*" in allowed:
        return True
    endpoint = channel_endpoint(channel_name)
    return endpoint is not None and endpoint in allowed


def direct_carrier_inbound_admitted(
    *, screening_required: bool, env: Optional[dict] = None
) -> bool:
    """Whether a provider webhook may start an inbound run."""
    if not screening_required:
        return True
    source = os.environ if env is None else env
    return (
        source.get("DIRECT_CARRIER_INBOUND_ENABLED", "false").strip().lower() == "true"
    )
