"""Inbound screening is the organization's call, and off by default.

Hopwhistle screens every external caller against the TCPA litigator list and
hands the approved ones to Dograh over the ``hopwhistle-callback`` endpoint.
With the org's "Answer only callers screened by Hopwhistle" setting on, a call
reaching Stasis any other way, or a provider webhook straight from the PSTN, is
refused before any workflow run exists. With it off (the default) every inbound
call is answered.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from api.errors.telephony_errors import TelephonyError
from api.schemas.organization_preferences import OrganizationPreferences
from api.services.telephony import ari_manager, inbound_admission
from api.services.telephony.ari_manager import ARIConnection
from api.services.telephony.inbound_admission import (
    ari_inbound_admitted,
    channel_endpoint,
    direct_carrier_inbound_admitted,
    screening_required,
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


# --- the setting ------------------------------------------------------------


def test_screening_is_off_by_default():
    assert OrganizationPreferences().inbound_screening_required is False


async def test_screening_required_reads_the_org_setting():
    with patch.object(
        inbound_admission,
        "inbound_screening_required",
        AsyncMock(side_effect=lambda org: org == 7),
    ):
        assert await screening_required(7) is True
        assert await screening_required(8) is False


async def test_an_unreadable_setting_is_treated_as_on():
    with patch.object(
        inbound_admission,
        "inbound_screening_required",
        AsyncMock(side_effect=RuntimeError("db down")),
    ):
        assert await screening_required(7) is True


# --- screening off: everything is answered ----------------------------------


@pytest.mark.parametrize(
    "name",
    ["PJSIP/hopwhistle-callback-0000002a", "PJSIP/fractel-00000001", "", None],
)
def test_with_screening_off_every_ari_call_is_admitted(name):
    assert ari_inbound_admitted(name, screening_required=False, env={})


def test_with_screening_off_provider_webhooks_are_admitted():
    assert direct_carrier_inbound_admitted(screening_required=False, env={})


# --- screening on: only what Hopwhistle handed over --------------------------


def test_with_screening_on_only_the_hopwhistle_endpoint_is_admitted():
    def admitted(name):
        return ari_inbound_admitted(name, screening_required=True, env={})

    assert admitted("PJSIP/hopwhistle-callback-0000002a")
    assert not admitted("PJSIP/fractel-00000001")
    assert not admitted("PJSIP/twilio-00000001")
    assert not admitted("")
    assert not admitted(None)


def test_the_allowlist_is_configurable():
    def admitted(name, env):
        return ari_inbound_admitted(name, screening_required=True, env=env)

    env = {"ARI_INBOUND_ALLOWED_ENDPOINTS": "hopwhistle-callback, hw-two"}
    assert admitted("PJSIP/hw-two-00000003", env)
    assert not admitted("PJSIP/fractel-00000001", env)
    assert admitted("PJSIP/fractel-00000001", {"ARI_INBOUND_ALLOWED_ENDPOINTS": "*"})
    # An empty list admits nothing rather than everything.
    assert not admitted(
        "PJSIP/hopwhistle-callback-1", {"ARI_INBOUND_ALLOWED_ENDPOINTS": ""}
    )


def test_with_screening_on_provider_webhooks_need_the_env_opt_in():
    def admitted(env):
        return direct_carrier_inbound_admitted(screening_required=True, env=env)

    assert not admitted({})
    assert not admitted({"DIRECT_CARRIER_INBOUND_ENABLED": "1"})
    assert admitted({"DIRECT_CARRIER_INBOUND_ENABLED": "true"})


# --- ARI inbound ------------------------------------------------------------


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


def _ari_db():
    # No number registered: the existing "hang up" path, reached only when
    # admission lets the call through.
    return SimpleNamespace(
        find_active_phone_number_for_inbound=AsyncMock(return_value=None),
        get_workflow=AsyncMock(),
        create_workflow_run=AsyncMock(),
    )


async def test_screening_on_hangs_up_a_call_not_from_hopwhistle_before_any_run(
    monkeypatch,
):
    db = _ari_db()
    monkeypatch.setattr(ari_manager, "db_client", db)
    monkeypatch.setattr(ari_manager, "screening_required", AsyncMock(return_value=True))
    monkeypatch.delenv("ARI_INBOUND_ALLOWED_ENDPOINTS", raising=False)
    conn = _ARIConnection()

    await conn._handle_inbound_stasis_start(
        "chan-1", "Ring", _ring("PJSIP/fractel-00000001")
    )

    assert conn.deleted_channels == ["chan-1"]
    assert conn.answered == []
    db.find_active_phone_number_for_inbound.assert_not_called()
    db.create_workflow_run.assert_not_called()
    ari_manager.screening_required.assert_awaited_once_with(1)


async def test_screening_on_lets_a_hopwhistle_call_resolve_its_agent(monkeypatch):
    db = _ari_db()
    monkeypatch.setattr(ari_manager, "db_client", db)
    monkeypatch.setattr(ari_manager, "screening_required", AsyncMock(return_value=True))
    monkeypatch.delenv("ARI_INBOUND_ALLOWED_ENDPOINTS", raising=False)
    conn = _ARIConnection()

    await conn._handle_inbound_stasis_start(
        "chan-1", "Ring", _ring("PJSIP/hopwhistle-callback-0000002a")
    )

    db.find_active_phone_number_for_inbound.assert_awaited_once()


async def test_screening_off_lets_any_call_resolve_its_agent(monkeypatch):
    db = _ari_db()
    monkeypatch.setattr(ari_manager, "db_client", db)
    monkeypatch.setattr(
        ari_manager, "screening_required", AsyncMock(return_value=False)
    )
    monkeypatch.delenv("ARI_INBOUND_ALLOWED_ENDPOINTS", raising=False)
    conn = _ARIConnection()

    await conn._handle_inbound_stasis_start(
        "chan-1", "Ring", _ring("PJSIP/fractel-00000001")
    )

    db.find_active_phone_number_for_inbound.assert_awaited_once()


# --- provider webhooks ------------------------------------------------------


async def _post_inbound_run(monkeypatch, *, screening_on: bool):
    from api.routes.telephony import handle_inbound_run

    monkeypatch.delenv("DIRECT_CARRIER_INBOUND_ENABLED", raising=False)
    provider_class = SimpleNamespace(
        PROVIDER_NAME="twilio",
        generate_validation_error_response=Mock(
            return_value=SimpleNamespace(body=b"validation-error")
        ),
    )
    normalized = SimpleNamespace(
        provider="twilio",
        direction="inbound",
        to_number="+18885550103",
        from_number="+14235551212",
        account_id="AC123",
        to_country=None,
    )
    config = SimpleNamespace(id=10, organization_id=1)
    phone_row = SimpleNamespace(inbound_workflow_id=None)
    request = SimpleNamespace(headers={}, url="https://api.example.com/inbound/run")
    screening = AsyncMock(return_value=screening_on)
    with (
        patch(
            "api.routes.telephony.parse_webhook_request",
            new=AsyncMock(return_value=({}, b"")),
        ),
        patch(
            "api.routes.telephony._detect_provider",
            new=AsyncMock(return_value=provider_class),
        ),
        patch("api.routes.telephony.normalize_webhook_data", return_value=normalized),
        patch("api.routes.telephony.screening_required", new=screening),
        patch("api.routes.telephony.db_client") as db,
    ):
        db.find_inbound_route_by_account = AsyncMock(return_value=(config, phone_row))
        db.get_workflow = AsyncMock()
        response = await handle_inbound_run(request)
    return response, db, screening, provider_class


async def test_screening_on_refuses_a_provider_webhook_before_any_run(monkeypatch):
    response, db, screening, provider = await _post_inbound_run(
        monkeypatch, screening_on=True
    )

    assert b"<Hangup/>" in response.body
    screening.assert_awaited_once_with(1)
    db.get_workflow.assert_not_called()
    provider.generate_validation_error_response.assert_not_called()


async def test_screening_off_lets_a_provider_webhook_through(monkeypatch):
    response, _, _, provider = await _post_inbound_run(monkeypatch, screening_on=False)

    # Past admission: the (unassigned) number gets its normal handling.
    assert response.body == b"validation-error"
    provider.generate_validation_error_response.assert_called_once_with(
        TelephonyError.WORKFLOW_NOT_FOUND
    )
