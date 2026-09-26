"""Tests for do-not-call suppression."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.db.models import OrganizationModel
from api.schemas.workflow_configurations import (
    DNCConfiguration,
    WorkflowConfigurationDefaults,
)
from api.services.dnc import (
    SOURCE_AGENT,
    SOURCE_DISPOSITION,
    counterparty_number,
    disposition_requests_suppression,
    dnc_service,
    normalize_dnc_number,
    normalize_dnc_numbers,
    phone_number_from_context,
)
from api.services.dnc.tool import add_caller_to_dnc


class TestNormalization:
    @pytest.mark.parametrize(
        "raw",
        [
            "5551234567",
            "555-123-4567",
            "(555) 123-4567",
            "555.123.4567",
            " 555 123 4567 ",
            "15551234567",
            "1-555-123-4567",
            "+15551234567",
            "+1 (555) 123-4567",
        ],
    )
    def test_nanp_shapes_collapse_to_one_key(self, raw):
        """The same person appears in every one of these forms across lists."""
        assert normalize_dnc_number(raw) == "+15551234567"

    def test_international_number_keeps_its_country_code(self):
        assert normalize_dnc_number("+44 7700 900123") == "+447700900123"
        assert normalize_dnc_number("447700900123") == "+447700900123"

    @pytest.mark.parametrize(
        "raw",
        [None, "", "   ", "not a number", "abc", "123", "1234567", "-", "+"],
    )
    def test_unusable_input_is_rejected(self, raw):
        assert normalize_dnc_number(raw) is None

    def test_over_length_input_is_rejected(self):
        # E.164 tops out at 15 digits; anything longer is a typo or a join of
        # two numbers, and suppressing it would be meaningless either way.
        assert normalize_dnc_number("1" * 16) is None

    def test_ten_digits_starting_zero_or_one_is_not_nanp(self):
        # NANP area codes never start with 0 or 1, so these fall through to
        # the plain-digits branch rather than being given a bogus +1.
        assert normalize_dnc_number("0551234567") == "+0551234567"
        assert normalize_dnc_number("1551234567") == "+1551234567"

    def test_numbers_are_accepted_deduplicated_and_ordered(self):
        accepted, rejected = normalize_dnc_numbers(
            [
                "555-123-4567",
                "+15551234567",  # same number, different spelling
                "5559876543",
                "garbage",
                "",
                None,
            ]
        )
        assert accepted == ["+15551234567", "+15559876543"]
        assert rejected == ["garbage"]

    def test_integers_are_accepted(self):
        # CSV parsers hand back ints for a column of bare digits.
        assert normalize_dnc_number(5551234567) == "+15551234567"


class TestCounterpartyNumber:
    def test_outbound_uses_the_number_we_dialled(self):
        context = {"called_number": "+15551234567", "caller_number": "+15550001111"}
        assert counterparty_number(context) == "+15551234567"

    def test_inbound_uses_the_number_that_called_us(self):
        """Suppressing our own caller ID would silently kill the campaign."""
        context = {
            "called_number": "+15550001111",
            "caller_number": "+15551234567",
            "direction": "inbound",
        }
        assert counterparty_number(context) == "+15551234567"

    def test_call_type_beats_context_direction(self):
        # Some providers leave `direction` unset or wrong on outbound.
        context = {
            "called_number": "+15550001111",
            "caller_number": "+15551234567",
            "direction": "outbound",
        }
        assert counterparty_number(context, "INBOUND") == "+15551234567"

    def test_falls_back_to_the_lead_phone_number(self):
        assert counterparty_number({"phone_number": "5551234567"}) == "5551234567"

    def test_returns_none_when_there_is_no_number(self):
        assert counterparty_number({}) is None
        assert counterparty_number(None) is None


class TestDispositionAndContextHelpers:
    @pytest.mark.parametrize("value", ["DNC", "dnc", " Dnc "])
    def test_dnc_disposition_requests_suppression(self, value):
        assert disposition_requests_suppression(value) is True

    @pytest.mark.parametrize("value", ["XFER", "NI", "", None, "VOICEMAIL"])
    def test_other_dispositions_do_not(self, value):
        assert disposition_requests_suppression(value) is False

    def test_phone_number_is_found_under_any_known_key(self):
        assert phone_number_from_context({"phone_number": "1"}) == "1"
        assert phone_number_from_context({"phoneNumber": "2"}) == "2"
        assert phone_number_from_context({"to_number": "3"}) == "3"
        assert phone_number_from_context({}) is None
        assert phone_number_from_context(None) is None


class TestConfiguration:
    def test_agent_tool_is_on_by_default(self):
        """A caller asking not to be contacted must be heard by default."""
        assert WorkflowConfigurationDefaults().dnc.agent_tool_enabled is True

    def test_agent_tool_can_be_turned_off(self):
        assert DNCConfiguration(agent_tool_enabled=False).agent_tool_enabled is False

    def test_explicit_null_falls_back_to_defaults(self):
        config = WorkflowConfigurationDefaults(**{"dnc": None})
        assert config.dnc.agent_tool_enabled is True


@pytest.fixture(scope="module")
async def dnc_db(setup_test_database):
    """A real session factory: the client commits, so savepoints won't do."""
    from api.db import db_client

    engine = create_async_engine(setup_test_database, echo=False)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    original_engine = db_client.engine
    original_session = db_client.async_session
    db_client.engine = engine
    db_client.async_session = session_factory

    yield session_factory

    db_client.engine = original_engine
    db_client.async_session = original_session
    await engine.dispose()


@pytest.fixture
async def organization_id(dnc_db) -> int:
    async with dnc_db() as session:
        org = OrganizationModel(provider_id=f"test-org-{uuid.uuid4().hex[:8]}")
        session.add(org)
        await session.commit()
        return org.id


@pytest.fixture
async def other_organization_id(dnc_db) -> int:
    async with dnc_db() as session:
        org = OrganizationModel(provider_id=f"test-org-{uuid.uuid4().hex[:8]}")
        session.add(org)
        await session.commit()
        return org.id


class TestSuppressionStorage:
    async def test_add_then_match_across_formats(self, organization_id):
        """Stored one way, looked up another — the canonical key bridges them."""
        result = await dnc_service.add_numbers(
            organization_id=organization_id, raw_numbers=["(555) 111-2222"]
        )
        assert result.added == 1

        for spelling in ["5551112222", "+15551112222", "1-555-111-2222"]:
            assert await dnc_service.is_suppressed(
                organization_id=organization_id, raw_number=spelling
            )

    async def test_unlisted_number_is_not_suppressed(self, organization_id):
        assert not await dnc_service.is_suppressed(
            organization_id=organization_id, raw_number="5559998888"
        )

    async def test_unparseable_number_is_not_suppressed(self, organization_id):
        """Failing closed on junk would kill every campaign using an odd format."""
        assert not await dnc_service.is_suppressed(
            organization_id=organization_id, raw_number="not a number"
        )

    async def test_re_adding_updates_rather_than_duplicating(self, organization_id):
        first = await dnc_service.add_numbers(
            organization_id=organization_id,
            raw_numbers=["5552223333"],
            reason="first",
        )
        second = await dnc_service.add_numbers(
            organization_id=organization_id,
            raw_numbers=["555-222-3333"],
            reason="second",
        )
        assert first.added == 1
        assert second.added == 0
        assert second.already_listed == 1

        from api.db import db_client

        entries = await db_client.list_dnc_entries(
            organization_id=organization_id, search="5552223333"
        )
        assert len(entries) == 1
        assert entries[0].reason == "second"

    async def test_invalid_rows_are_reported_not_fatal(self, organization_id):
        result = await dnc_service.add_numbers(
            organization_id=organization_id,
            raw_numbers=["5554445555", "see note", "123"],
        )
        assert result.added == 1
        assert sorted(result.invalid) == ["123", "see note"]

    async def test_all_invalid_input_adds_nothing(self, organization_id):
        result = await dnc_service.add_numbers(
            organization_id=organization_id, raw_numbers=["nope", ""]
        )
        assert result.added == 0
        assert result.invalid == ["nope"]

    async def test_list_is_scoped_to_one_organization(
        self, organization_id, other_organization_id
    ):
        """A shared list would leak who a competitor has been calling."""
        await dnc_service.add_numbers(
            organization_id=organization_id, raw_numbers=["5556667777"]
        )
        assert await dnc_service.is_suppressed(
            organization_id=organization_id, raw_number="5556667777"
        )
        assert not await dnc_service.is_suppressed(
            organization_id=other_organization_id, raw_number="5556667777"
        )

    async def test_expired_entry_stops_suppressing(self, organization_id):
        await dnc_service.add_numbers(
            organization_id=organization_id,
            raw_numbers=["5557778888"],
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )
        assert not await dnc_service.is_suppressed(
            organization_id=organization_id, raw_number="5557778888"
        )

    async def test_future_expiry_still_suppresses(self, organization_id):
        await dnc_service.add_numbers(
            organization_id=organization_id,
            raw_numbers=["5557779999"],
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
        assert await dnc_service.is_suppressed(
            organization_id=organization_id, raw_number="5557779999"
        )

    async def test_remove_takes_a_number_off_the_list(self, organization_id):
        await dnc_service.add_numbers(
            organization_id=organization_id, raw_numbers=["5558889999"]
        )
        assert await dnc_service.remove_number(
            organization_id=organization_id, raw_number="(555) 888-9999"
        )
        assert not await dnc_service.is_suppressed(
            organization_id=organization_id, raw_number="5558889999"
        )

    async def test_removing_an_absent_number_reports_false(self, organization_id):
        assert not await dnc_service.remove_number(
            organization_id=organization_id, raw_number="5550000000"
        )

    async def test_partition_returns_the_callers_own_spellings(self, organization_id):
        """A dispatch batch has to match results back to its own leads."""
        await dnc_service.add_numbers(
            organization_id=organization_id, raw_numbers=["5551230001"]
        )
        suppressed = await dnc_service.partition_suppressed(
            organization_id=organization_id,
            raw_numbers=["(555) 123-0001", "5559990002", "junk"],
        )
        assert suppressed == {"(555) 123-0001"}

    async def test_partition_of_nothing_queries_nothing(self, organization_id):
        assert (
            await dnc_service.partition_suppressed(
                organization_id=organization_id, raw_numbers=[]
            )
            == set()
        )


class TestDispositionCapture:
    async def test_dnc_disposition_suppresses_the_number(self, organization_id):
        added = await dnc_service.record_disposition(
            organization_id=organization_id,
            raw_number="5551119999",
            disposition="DNC",
            workflow_run_id=None,
        )
        assert added
        assert await dnc_service.is_suppressed(
            organization_id=organization_id, raw_number="5551119999"
        )

    async def test_other_dispositions_do_not_suppress(self, organization_id):
        added = await dnc_service.record_disposition(
            organization_id=organization_id,
            raw_number="5551118888",
            disposition="XFER",
        )
        assert not added
        assert not await dnc_service.is_suppressed(
            organization_id=organization_id, raw_number="5551118888"
        )

    async def test_source_records_how_it_got_there(self, organization_id):
        from api.db import db_client

        await dnc_service.record_disposition(
            organization_id=organization_id,
            raw_number="5551117777",
            disposition="DNC",
        )
        await dnc_service.add_from_agent(
            organization_id=organization_id, raw_number="5551116666"
        )

        by_number = {
            entry.phone_number: entry.source
            for entry in await db_client.list_dnc_entries(
                organization_id=organization_id
            )
        }
        assert by_number["+15551117777"] == SOURCE_DISPOSITION
        assert by_number["+15551116666"] == SOURCE_AGENT


class TestAgentTool:
    async def test_suppresses_the_number_on_the_call(self, organization_id):
        result = await add_caller_to_dnc(
            organization_id=organization_id,
            call_context_vars={"called_number": "5552221111"},
            reason="asked to be removed",
        )
        assert result["status"] == "ok"
        assert await dnc_service.is_suppressed(
            organization_id=organization_id, raw_number="5552221111"
        )

    async def test_reports_when_already_listed(self, organization_id):
        context = {"called_number": "5552223333"}
        await add_caller_to_dnc(
            organization_id=organization_id, call_context_vars=context
        )
        result = await add_caller_to_dnc(
            organization_id=organization_id, call_context_vars=context
        )
        assert result["status"] == "ok"
        assert result["already_listed"] is True

    async def test_missing_organization_is_an_error_not_a_crash(self):
        result = await add_caller_to_dnc(
            organization_id=None, call_context_vars={"called_number": "5551112222"}
        )
        assert result["status"] == "error"

    async def test_call_without_a_number_is_an_error(self, organization_id):
        result = await add_caller_to_dnc(
            organization_id=organization_id, call_context_vars={}
        )
        assert result["status"] == "error"

    async def test_unusable_number_is_an_error(self, organization_id):
        result = await add_caller_to_dnc(
            organization_id=organization_id,
            call_context_vars={"called_number": "anonymous"},
        )
        assert result["status"] == "error"

    def test_tool_schema_does_not_accept_a_phone_number(self):
        """The model must never choose which number gets suppressed."""
        from api.services.dnc.tool import get_add_to_dnc_tool_schema

        schema = get_add_to_dnc_tool_schema()
        assert schema.name == "add_to_do_not_call"
        # Only a free-text reason — no number for the model to get wrong.
        assert set(schema.properties) == {"reason"}
        assert schema.required == []


class TestDispatcherGate:
    def _queued_run(self, run_id: int, phone_number: str | None):
        from api.db.models import QueuedRunModel

        return QueuedRunModel(
            id=run_id,
            campaign_id=1,
            source_uuid=f"src-{run_id}",
            context_variables=({"phone_number": phone_number} if phone_number else {}),
            state="processing",
        )

    async def test_suppressed_lead_is_skipped_and_marked(self):
        from api.services.campaign.campaign_call_dispatcher import (
            CampaignCallDispatcher,
        )

        dispatcher = CampaignCallDispatcher()
        queued_run = self._queued_run(1, "5551234567")

        with patch(
            "api.services.campaign.campaign_call_dispatcher.db_client"
        ) as db_mock:
            db_mock.update_queued_run = AsyncMock()
            skipped = await dispatcher._skip_suppressed(queued_run, {"5551234567"})

        assert skipped is True
        # 'suppressed', not 'failed': a compliance skip is not a dialling error.
        assert db_mock.update_queued_run.await_args.kwargs["state"] == "suppressed"

    async def test_unsuppressed_lead_proceeds(self):
        from api.services.campaign.campaign_call_dispatcher import (
            CampaignCallDispatcher,
        )

        dispatcher = CampaignCallDispatcher()
        queued_run = self._queued_run(2, "5559998888")
        assert await dispatcher._skip_suppressed(queued_run, {"5551234567"}) is False

    async def test_lead_without_a_number_proceeds(self):
        from api.services.campaign.campaign_call_dispatcher import (
            CampaignCallDispatcher,
        )

        dispatcher = CampaignCallDispatcher()
        queued_run = self._queued_run(3, None)
        assert await dispatcher._skip_suppressed(queued_run, {"5551234567"}) is False

    async def test_failure_to_mark_still_skips_the_dial(self):
        """Not dialling is the safe failure — the run is retried next batch."""
        from api.services.campaign.campaign_call_dispatcher import (
            CampaignCallDispatcher,
        )

        dispatcher = CampaignCallDispatcher()
        queued_run = self._queued_run(4, "5551234567")

        with patch(
            "api.services.campaign.campaign_call_dispatcher.db_client"
        ) as db_mock:
            db_mock.update_queued_run = AsyncMock(side_effect=RuntimeError("db down"))
            skipped = await dispatcher._skip_suppressed(queued_run, {"5551234567"})

        assert skipped is True

    async def test_batch_lookup_queries_once_for_every_lead(self, organization_id):
        from api.services.campaign.campaign_call_dispatcher import (
            CampaignCallDispatcher,
        )

        await dnc_service.add_numbers(
            organization_id=organization_id, raw_numbers=["5551110001"]
        )
        dispatcher = CampaignCallDispatcher()
        runs = [
            self._queued_run(1, "5551110001"),
            self._queued_run(2, "5551110002"),
            self._queued_run(3, None),
        ]

        suppressed = await dispatcher._suppressed_numbers_in_batch(
            organization_id, runs
        )
        assert suppressed == {"5551110001"}

    async def test_batch_lookup_with_no_numbers_hits_no_database(self):
        from api.services.campaign.campaign_call_dispatcher import (
            CampaignCallDispatcher,
        )

        dispatcher = CampaignCallDispatcher()
        runs = [self._queued_run(1, None), self._queued_run(2, None)]
        assert await dispatcher._suppressed_numbers_in_batch(999, runs) == set()
