"""Dograh answers only inbound callers Hopwhistle has already screened.

Hopwhistle screens every external caller against the TCPA litigator list and
hands the approved ones to Dograh over the ``hopwhistle-callback`` endpoint.
A call reaching Stasis any other way, or a provider webhook straight from the
PSTN, is refused before any workflow run exists.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.services.telephony import ari_manager
from api.services.telephony.ari_manager import ARIConnection
from api.services.telephony.inbound_admission import (
    ari_inbound_admitted,
    channel_endpoint,
    direct_carrier_inbound_admitted,
)


@pytest.mark.parametrize(
    "name, endpoint",
    [
        ("PJSIP/hopwhistle-callback-0000002a", "hopwhistle-callback"),
        ("PJSIP/fractel-00000001", "fractel"),
        ("SIP/hopwhistle-callback-ab12cd34", "hopwhistle-callback"),
        ("", None),
        (None, None),
        ("garbage", None),
    ],
)
def test_channel_endpoint(name, endpoint):
    assert channel_endpoint(name) == endpoint


def test_by_default_only_the_hopwhistle_endpoint_is_admitted():
    assert ari_inbound_admitted("PJSIP/hopwhistle-callback-0000002a", env={})
    assert not ari_inbound_admitted("PJSIP/fractel-00000001", env={})
    assert not ari_inbound_admitted("PJSIP/twilio-00000001", env={})
    assert not ari_inbound_admitted("", env={})
    assert not ari_inbound_admitted(None, env={})


def test_the_allowlist_is_configurable():
    env = {"ARI_INBOUND_ALLOWED_ENDPOINTS": "hopwhistle-callback, hw-two"}
    assert ari_inbound_admitted("PJSIP/hw-two-00000003", env=env)
    assert not ari_inbound_admitted("PJSIP/fractel-00000001", env=env)
    assert ari_inbound_admitted(
        "PJSIP/fractel-00000001", env={"ARI_INBOUND_ALLOWED_ENDPOINTS": "*"}
    )
    # An empty list admits nothing rather than everything.
    assert not ari_inbound_admitted(
        "PJSIP/hopwhistle-callback-1", env={"ARI_INBOUND_ALLOWED_ENDPOINTS": ""}
    )


def test_direct_carrier_inbound_is_off_unless_enabled():
    assert not direct_carrier_inbound_admitted(env={})
    assert not direct_carrier_inbound_admitted(
        env={"DIRECT_CARRIER_INBOUND_ENABLED": "1"}
    )
    assert direct_carrier_inbound_admitted(
        env={"DIRECT_CARRIER_INBOUND_ENABLED": "true"}
    )


class _ARIConnection(ARIConnection):
    def __init__(self):
        super().__init__(
            organization_id=1,
            telephony_configuration_id=10,
            ari_endpoint="http://asterisk.test:8088",
            app_name="dograh",
            app_password="secret",
        )
        self.deleted_channels = []
        self.answered = []

    async def _delete_channel(self, channel_id: str):
        self.deleted_channels.append(channel_id)

    async def _answer_channel(self, channel_id: str):
        self.answered.append(channel_id)


def _ring(name: str) -> dict:
    return {
        "channel": {
            "id": "chan-1",
            "name": name,
            "caller": {"number": "+14235551212"},
            "dialplan": {"exten": "+18885550103", "context": "from-fractel"},
        }
    }


async def test_a_call_that_did_not_come_from_hopwhistle_is_hung_up_before_any_run(
    monkeypatch,
):
    db = SimpleNamespace(
        find_active_phone_number_for_inbound=AsyncMock(),
        get_workflow=AsyncMock(),
        create_workflow_run=AsyncMock(),
    )
    monkeypatch.setattr(ari_manager, "db_client", db)
    monkeypatch.delenv("ARI_INBOUND_ALLOWED_ENDPOINTS", raising=False)
    conn = _ARIConnection()

    await conn._handle_inbound_stasis_start(
        "chan-1", "Ring", _ring("PJSIP/fractel-00000001")
    )

    assert conn.deleted_channels == ["chan-1"]
    assert conn.answered == []
    db.find_active_phone_number_for_inbound.assert_not_called()
    db.create_workflow_run.assert_not_called()


async def test_a_call_handed_over_by_hopwhistle_goes_on_to_resolve_its_agent(
    monkeypatch,
):
    db = SimpleNamespace(
        # No number registered: the existing "hang up" path, reached only
        # because admission let the call through.
        find_active_phone_number_for_inbound=AsyncMock(return_value=None),
        create_workflow_run=AsyncMock(),
    )
    monkeypatch.setattr(ari_manager, "db_client", db)
    monkeypatch.delenv("ARI_INBOUND_ALLOWED_ENDPOINTS", raising=False)
    conn = _ARIConnection()

    await conn._handle_inbound_stasis_start(
        "chan-1", "Ring", _ring("PJSIP/hopwhistle-callback-0000002a")
    )

    db.find_active_phone_number_for_inbound.assert_awaited_once()


async def test_a_provider_webhook_is_refused_before_anything_is_read(monkeypatch):
    from unittest.mock import patch

    from api.routes.telephony import handle_inbound_run

    monkeypatch.delenv("DIRECT_CARRIER_INBOUND_ENABLED", raising=False)
    request = SimpleNamespace(headers={}, url="https://api.example.com/inbound/run")
    with (
        patch("api.routes.telephony.parse_webhook_request", new=AsyncMock()) as parse,
        patch("api.routes.telephony.db_client") as db,
    ):
        response = await handle_inbound_run(request)

    assert b"<Hangup/>" in response.body
    parse.assert_not_awaited()
    db.find_inbound_route_by_account.assert_not_called()
