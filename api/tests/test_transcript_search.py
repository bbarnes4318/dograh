"""Database-backed tests for transcript full-text search.

These exercise the real Postgres query — ``websearch_to_tsquery``, the
``@@`` match and ``ts_headline`` — because that is where the behaviour
actually lives.
"""

import pytest

from api.db.models import OrganizationModel, UserModel
from api.utils.transcript import generate_searchable_transcript

GRAPH = {
    "nodes": [
        {
            "id": "1",
            "type": "startCall",
            "position": {"x": 0, "y": 0},
            "data": {"name": "Start", "prompt": "hi", "is_start": True},
        },
        {
            "id": "2",
            "type": "endCall",
            "position": {"x": 200, "y": 0},
            "data": {"name": "End", "prompt": "bye"},
        },
    ],
    "edges": [{"id": "e1", "source": "1", "target": "2", "data": {"label": "End"}}],
}


@pytest.fixture
async def org_and_user(async_session):
    org = OrganizationModel(provider_id="test-org-transcript-search")
    async_session.add(org)
    await async_session.flush()

    user = UserModel(
        provider_id="test-user-transcript-search", selected_organization_id=org.id
    )
    async_session.add(user)
    await async_session.flush()
    return org, user


@pytest.fixture
async def workflow(db_session, org_and_user):
    org, user = org_and_user
    return await db_session.create_workflow(
        name="Search Workflow",
        workflow_definition=GRAPH,
        user_id=user.id,
        organization_id=org.id,
    )


async def _run_with_transcript(db_session, workflow, user, transcript: str):
    run = await db_session.create_workflow_run(
        name="WR-SEARCH",
        workflow_id=workflow.id,
        mode="twilio",
        user_id=user.id,
        organization_id=workflow.organization_id,
    )
    await db_session.update_workflow_run(run_id=run.id, transcript_text=transcript)
    return run


class TestGenerateSearchableTranscript:
    def test_keeps_speakers_and_drops_timestamps(self):
        events = [
            {
                "type": "rtf-user-transcription",
                "payload": {
                    "text": "this is too expensive",
                    "final": True,
                    "timestamp": "2026-06-15T10:00:00Z",
                },
                "timestamp": "2026-06-15T10:00:00Z",
            },
            {
                "type": "rtf-bot-text",
                "payload": {"text": "I understand", "timestamp": "x"},
            },
        ]
        assert generate_searchable_transcript(events) == (
            "user: this is too expensive\nassistant: I understand"
        )

    def test_skips_interim_user_transcriptions(self):
        events = [
            {
                "type": "rtf-user-transcription",
                "payload": {"text": "too", "final": False},
            },
            {
                "type": "rtf-user-transcription",
                "payload": {"text": "too much", "final": True},
            },
        ]
        assert generate_searchable_transcript(events) == "user: too much"

    def test_skips_empty_text(self):
        events = [
            {
                "type": "rtf-user-transcription",
                "payload": {"text": "  ", "final": True},
            },
        ]
        assert generate_searchable_transcript(events) == ""

    def test_no_events(self):
        assert generate_searchable_transcript([]) == ""


class TestSearchRunTranscripts:
    async def test_finds_a_phrase(self, db_session, workflow, org_and_user):
        _, user = org_and_user
        run = await _run_with_transcript(
            db_session,
            workflow,
            user,
            "user: honestly this is too expensive for us\nassistant: I hear you",
        )
        await _run_with_transcript(
            db_session, workflow, user, "user: sounds great, sign me up"
        )

        results, total = await db_session.search_run_transcripts(
            organization_id=workflow.organization_id, query_text='"too expensive"'
        )

        assert total == 1
        assert results[0]["id"] == run.id
        assert "expensive" in results[0]["excerpt"].lower()

    async def test_stemming_matches_related_words(
        self, db_session, workflow, org_and_user
    ):
        _, user = org_and_user
        await _run_with_transcript(
            db_session, workflow, user, "user: your pricing is way out of line"
        )

        _, total = await db_session.search_run_transcripts(
            organization_id=workflow.organization_id, query_text="priced"
        )

        assert total == 1

    async def test_excludes_with_a_leading_minus(
        self, db_session, workflow, org_and_user
    ):
        _, user = org_and_user
        await _run_with_transcript(
            db_session, workflow, user, "user: too expensive but interested"
        )
        await _run_with_transcript(
            db_session, workflow, user, "user: too expensive, not interested"
        )

        _, total = await db_session.search_run_transcripts(
            organization_id=workflow.organization_id,
            query_text="expensive -interested",
        )

        assert total == 0

    async def test_does_not_leak_across_organizations(
        self, db_session, workflow, org_and_user, async_session
    ):
        _, user = org_and_user
        await _run_with_transcript(
            db_session, workflow, user, "user: too expensive for me"
        )

        other_org = OrganizationModel(provider_id="test-org-transcript-other")
        async_session.add(other_org)
        await async_session.flush()

        _, total = await db_session.search_run_transcripts(
            organization_id=other_org.id, query_text="expensive"
        )

        assert total == 0

    async def test_runs_without_a_transcript_never_match(
        self, db_session, workflow, org_and_user
    ):
        _, user = org_and_user
        await db_session.create_workflow_run(
            name="WR-NO-TRANSCRIPT",
            workflow_id=workflow.id,
            mode="twilio",
            user_id=user.id,
            organization_id=workflow.organization_id,
        )

        _, total = await db_session.search_run_transcripts(
            organization_id=workflow.organization_id, query_text="anything"
        )

        assert total == 0

    async def test_paginates(self, db_session, workflow, org_and_user):
        _, user = org_and_user
        for _ in range(3):
            await _run_with_transcript(
                db_session, workflow, user, "user: too expensive"
            )

        page, total = await db_session.search_run_transcripts(
            organization_id=workflow.organization_id,
            query_text="expensive",
            limit=2,
        )

        assert total == 3
        assert len(page) == 2

    async def test_filters_to_one_workflow(self, db_session, workflow, org_and_user):
        _, user = org_and_user
        await _run_with_transcript(db_session, workflow, user, "user: too expensive")

        _, total = await db_session.search_run_transcripts(
            organization_id=workflow.organization_id,
            query_text="expensive",
            workflow_id=workflow.id + 999,
        )

        assert total == 0
