"""Which inbound calls Dograh may answer.

Hopwhistle owns inbound compliance and routing. It screens every external
caller against the TCPA litigator list before it routes the call anywhere, and
only a caller it has approved is handed to Dograh -- over the
``hopwhistle-callback`` PJSIP endpoint that Hopwhistle's
``deploy/dograh/inbound-callback`` installs in Asterisk (it matches only
FreeSWITCH's address).

Dograh does not screen callers itself and must not try to. What it does here is
refuse any inbound call that did not come from Hopwhistle, because such a call
was never screened:

- an ARI call that entered Stasis from any other endpoint -- for example a
  carrier trunk whose Asterisk context hands calls straight to the Stasis app;
- a provider webhook (Twilio, Vonage, Telnyx, ...) starting an inbound run
  directly from the PSTN.

Configuration:

``ARI_INBOUND_ALLOWED_ENDPOINTS``
    Comma-separated PJSIP endpoint names an inbound ARI call may arrive on.
    Default ``hopwhistle-callback``. ``*`` admits every endpoint, which
    re-opens the unscreened path; only do that where no endpoint can carry
    external callers.

``DIRECT_CARRIER_INBOUND_ENABLED``
    ``true`` lets provider webhooks (``/inbound/run``, ``/inbound/{id}``)
    start inbound runs. Default ``false``: those calls never passed through
    Hopwhistle.
"""

import os
import re
from typing import Optional

DEFAULT_ARI_INBOUND_ENDPOINTS = "hopwhistle-callback"

# ``PJSIP/hopwhistle-callback-0000002a`` -> ``hopwhistle-callback``. Asterisk
# appends a hex sequence number to every channel of an endpoint.
_CHANNEL_NAME = re.compile(r"^[A-Za-z0-9_]+/(?P<endpoint>.+)-[0-9a-fA-F]+$")


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
    channel_name: Optional[str], env: Optional[dict] = None
) -> bool:
    """Whether an inbound ARI call on this channel came through Hopwhistle."""
    allowed = allowed_ari_inbound_endpoints(env)
    if "*" in allowed:
        return True
    endpoint = channel_endpoint(channel_name)
    return endpoint is not None and endpoint in allowed


def direct_carrier_inbound_admitted(env: Optional[dict] = None) -> bool:
    """Whether provider webhooks may start inbound runs at all."""
    source = os.environ if env is None else env
    return (
        source.get("DIRECT_CARRIER_INBOUND_ENABLED", "false").strip().lower() == "true"
    )
