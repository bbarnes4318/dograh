"""The platform block list (services/telephony/blocked_numbers.py)."""

import ast
from pathlib import Path

import pytest

from api.services.telephony.blocked_numbers import blocked_numbers, is_blocked_number

BLOCKED = "3253010810"
CLEAN = "4235551212"

API_DIR = Path(__file__).resolve().parents[2]


def test_list_holds_every_number():
    numbers = blocked_numbers()
    assert len(numbers) == 998
    assert all(len(n) == 10 and n.isdigit() for n in numbers)


@pytest.mark.parametrize(
    "raw",
    [BLOCKED, f"1{BLOCKED}", f"+1{BLOCKED}", "(325) 301-0810", "325.301.0810"],
)
def test_blocked_in_any_us_format(raw):
    assert is_blocked_number(raw)


@pytest.mark.parametrize(
    "raw",
    [CLEAN, f"+1{CLEAN}", f"+44{BLOCKED}", f"sip:{BLOCKED}@pbx", "", None, "unknown"],
)
def test_other_numbers_are_not_blocked(raw):
    assert not is_blocked_number(raw)


@pytest.mark.parametrize(
    "path, function",
    [
        # Outbound: before a run is started or a provider dials.
        ("services/campaign/campaign_call_dispatcher.py", "dispatch_call"),
        ("routes/public_agent.py", "_execute_resolved_target"),
        ("routes/telephony.py", "initiate_call"),
        # Inbound: before the call is answered.
        ("routes/telephony.py", "handle_inbound_run"),
        ("routes/telephony.py", "handle_inbound_telephony"),
        ("services/telephony/ari_manager.py", "_handle_inbound_stasis_start"),
    ],
)
def test_every_call_path_checks_the_list(path, function):
    tree = ast.parse((API_DIR / path).read_text())
    found = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function
    ]
    assert found, f"{function} not found in {path}"
    calls = {
        n.func.id
        for n in ast.walk(found[0])
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "is_blocked_number" in calls
