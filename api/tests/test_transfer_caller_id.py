"""The transfer leg presents the customer's number, not our own DID."""

import pytest

from api.services.sms.recipient import snapshot_call_parties
from api.services.workflow.pipecat_engine_custom_tools import (
    TRANSFER_CALLER_ID_ENV,
    transfer_caller_id,
)


@pytest.fixture(autouse=True)
def _switch_unset(monkeypatch):
    monkeypatch.delenv(TRANSFER_CALLER_ID_ENV, raising=False)


def test_outbound_call_presents_the_dialed_customer():
    parties = snapshot_call_parties(
        {"caller_number": "+18653456051", "called_number": "(615) 555-0142"}
    )
    assert transfer_caller_id(parties) == "+16155550142"


def test_inbound_call_presents_the_caller():
    parties = snapshot_call_parties(
        {
            "direction": "inbound",
            "caller_number": "16155550142",
            "called_number": "+18653456051",
        }
    )
    assert transfer_caller_id(parties) == "+16155550142"


def test_older_run_with_only_phone_number():
    assert transfer_caller_id({"phone_number": "6155550142"}) == "+16155550142"


@pytest.mark.parametrize(
    "parties",
    [
        {},
        None,
        {"direction": "inbound", "caller_number": "anonymous"},
        {"called_number": "12345"},
    ],
)
def test_no_usable_customer_leaves_the_provider_default(parties):
    assert transfer_caller_id(parties) is None


@pytest.mark.parametrize("value", ["false", "0", "off", "NO"])
def test_switch_off_leaves_the_provider_default(monkeypatch, value):
    monkeypatch.setenv(TRANSFER_CALLER_ID_ENV, value)
    assert transfer_caller_id({"called_number": "6155550142"}) is None
