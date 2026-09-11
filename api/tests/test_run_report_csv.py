import csv
from datetime import UTC, datetime
from types import SimpleNamespace

from api.services.reports.run_report import build_run_report_csv


def _run(**overrides):
    """A row shaped like the ones get_completed_runs_for_report selects."""
    defaults = dict(
        id=1,
        workflow_id=10,
        definition_id=100,
        campaign_id=None,
        created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        call_type="outbound",
        initial_context={"phone_number": "+15551234567"},
        gathered_context={"mapped_call_disposition": "XFER"},
        usage_info={"call_duration_seconds": 42.5},
        public_access_token=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _rows(runs):
    return list(csv.reader(build_run_report_csv(runs)))


def test_report_has_direction_column_next_to_phone_number():
    header = _rows([_run()])[0]

    assert "Direction" in header
    assert header.index("Direction") == header.index("Phone Number") + 1


def test_direction_reports_outbound_and_inbound():
    header, outbound, inbound = _rows(
        [_run(id=1, call_type="outbound"), _run(id=2, call_type="inbound")]
    )
    column = header.index("Direction")

    assert outbound[column] == "Outbound"
    assert inbound[column] == "Inbound"


def test_direction_stays_aligned_when_extracted_variables_are_present():
    """Extracted-variable columns are inserted after the fixed ones, so the
    Direction value must not drift into a neighbouring column."""
    header, row = _rows(
        [
            _run(
                gathered_context={
                    "mapped_call_disposition": "XFER",
                    "extracted_variables": {"lead_name": "Ada"},
                }
            )
        ]
    )

    assert row[header.index("Direction")] == "Outbound"
    assert row[header.index("lead_name")] == "Ada"
    assert row[header.index("Call Disposition")] == "XFER"


def test_missing_direction_does_not_break_the_report():
    """call_type is NOT NULL in the schema, but a blank must not raise."""
    header, row = _rows([_run(call_type=None)])

    assert row[header.index("Direction")] == ""
