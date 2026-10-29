"""Both inbound paths — ARI StasisStart and the provider ``/inbound/run``
webhook — pick the workflow through resolve_inbound_workflow_id and record its
decision on the run's initial_context."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from api.routes.telephony import handle_inbound_run
from api.services.telephony import ari_manager
from api.services.telephony.ari_manager import ARIConnection

ROUTING_META = {"group_id": 7, "workflow_id": 25, "source": "group"}


def _workflow(workflow_id):
    return SimpleNamespace(id=workflow_id, user_id=99, organization_id=11)


class _InboundARIConnection(ARIConnection):
    def __init__(self):
        super().__init__(
            organization_id=11,
            telephony_configuration_id=10,
            ari_endpoint="http://asterisk.test:8088",
            app_name="dograh",
            app_password="secret",
        )
        self.answered = []
        self.started = []
        self.deleted_channels = []

    async def _delete_channel(self, channel_id: str):
        self.deleted_channels.append(channel_id)

    async def _capture_external_pbx_call(self, channel_id, channel_name):
        return None

    async def _answer_channel(self, channel_id):
        self.answered.append(channel_id)

    async def _handle_stasis_start(self, channel_id, state, run_id, workflow_id):
        self.started.append((channel_id, run_id, workflow_id))


async def test_ari_inbound_uses_resolver(monkeypatch):
    phone_row = SimpleNamespace(
        id=501,
        organization_id=11,
        telephony_configuration_id=10,
        address="1000",
        inbound_workflow_id=15,
        inbound_routing_group_id=7,
    )
    fake_db = SimpleNamespace(
        find_active_phone_number_for_inbound=AsyncMock(return_value=phone_row),
        get_workflow=AsyncMock(side_effect=lambda wid, **_: _workflow(wid)),
        create_workflow_run=AsyncMock(return_value=SimpleNamespace(id=900)),
    )
    resolver = AsyncMock(return_value=(25, ROUTING_META))
    monkeypatch.setattr(ari_manager, "db_client", fake_db)
    monkeypatch.setattr(ari_manager, "resolve_inbound_workflow_id", resolver)
    monkeypatch.setattr(ari_manager, "ari_inbound_admitted", lambda *a, **k: True)
    monkeypatch.setattr(
        ari_manager, "screening_required", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        ari_manager,
        "call_concurrency",
        SimpleNamespace(
            acquire_org_slot=AsyncMock(return_value=object()),
            bind_workflow_run=AsyncMock(),
        ),
    )
    monkeypatch.setattr(
        ari_manager,
        "prepare_workflow_run_inputs",
        AsyncMock(return_value=SimpleNamespace(definition_id=3)),
    )
    monkeypatch.setattr(
        ari_manager,
        "authorize_workflow_run_start",
        AsyncMock(return_value=SimpleNamespace(has_quota=True)),
    )

    conn = _InboundARIConnection()
    event = {
        "channel": {
            "name": "PJSIP/hopwhistle-0001",
            "caller": {"number": "+15557650000"},
            "dialplan": {"exten": "1000"},
        }
    }
    await conn._handle_inbound_stasis_start("chan-1", "Ring", event)

    resolver.assert_awaited_once_with(phone_row, "+15557650000")
    kwargs = fake_db.create_workflow_run.await_args.kwargs
    assert kwargs["workflow_id"] == 25
    assert kwargs["initial_context"]["inbound_routing"] == ROUTING_META
    assert conn.started == [("chan-1", "900", "25")]
    assert conn.deleted_channels == []


async def test_webhook_inbound_run_uses_resolver():
    request = SimpleNamespace(headers={}, url="https://api.example.com/inbound/run")
    provider_class = SimpleNamespace(
        PROVIDER_NAME="twilio",
        generate_validation_error_response=Mock(return_value="error"),
    )
    normalized_data = SimpleNamespace(
        provider="twilio",
        direction="inbound",
        to_number="+15551230000",
        from_number="+15557650000",
        to_country="US",
        from_country="US",
        account_id="acct-1",
        call_id="call-1",
        raw_data={},
    )
    config = SimpleNamespace(id=55, organization_id=11)
    phone_row = SimpleNamespace(
        id=77, organization_id=11, inbound_workflow_id=15, inbound_routing_group_id=7
    )
    provider_instance = SimpleNamespace(
        verify_inbound_signature=AsyncMock(return_value=True),
        start_inbound_stream=AsyncMock(return_value="stream-response"),
    )
    resolver = AsyncMock(return_value=(25, ROUTING_META))

    with (
        patch(
            "api.routes.telephony.parse_webhook_request",
            new=AsyncMock(return_value=({}, "raw-body")),
        ),
        patch(
            "api.routes.telephony._detect_provider",
            new=AsyncMock(return_value=provider_class),
        ),
        patch(
            "api.routes.telephony.normalize_webhook_data",
            return_value=normalized_data,
        ),
        patch(
            "api.routes.telephony.screening_required",
            new=AsyncMock(return_value=False),
        ),
        patch("api.routes.telephony.resolve_inbound_workflow_id", new=resolver),
        patch("api.routes.telephony.db_client") as mock_db,
        patch(
            "api.routes.telephony.get_telephony_provider_by_id",
            new=AsyncMock(return_value=provider_instance),
        ),
        patch("api.routes.telephony.call_concurrency") as mock_concurrency,
        patch(
            "api.routes.telephony.prepare_workflow_run_inputs",
            new=AsyncMock(return_value=SimpleNamespace(definition_id=3)),
        ),
        patch(
            "api.routes.telephony.authorize_workflow_run_start",
            new=AsyncMock(return_value=SimpleNamespace(has_quota=True)),
        ),
        patch(
            "api.routes.telephony.get_backend_endpoints",
            new=AsyncMock(return_value=("https://api", "wss://api")),
        ),
    ):
        mock_db.find_inbound_route_by_account = AsyncMock(
            return_value=(config, phone_row)
        )
        mock_db.get_workflow = AsyncMock(side_effect=lambda wid, **_: _workflow(wid))
        mock_db.create_workflow_run = AsyncMock(return_value=SimpleNamespace(id=900))
        mock_concurrency.acquire_org_slot = AsyncMock(return_value=object())
        mock_concurrency.bind_workflow_run = AsyncMock()

        response = await handle_inbound_run(request)

    assert response == "stream-response"
    resolver.assert_awaited_once_with(phone_row, "+15557650000")
    args = mock_db.create_workflow_run.await_args
    assert args.args[1] == 25  # workflow_id
    assert args.kwargs["initial_context"]["inbound_routing"] == ROUTING_META
    ws_url = provider_instance.start_inbound_stream.await_args.kwargs["websocket_url"]
    assert "/telephony/ws/25/11/900" in ws_url
