"""Tests for the hopwhistle box patches ported into the fork.

Each of these used to be a file bind-mounted over the stock image on the
production box; now that the box runs an image built from this repo, their
behaviour has to be pinned here.
"""

import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.services.campaign.areacode_state import state_for_number
from api.services.campaign.campaign_call_dispatcher import (
    resolve_state_cid_policy,
    resolve_transfer_destination,
)
from api.services.campaign.rate_limiter import RateLimiter
from api.services.telephony.providers.ari.provider import _pjsip_endpoint
from api.services.workflow.pipecat_engine_callbacks import (
    create_max_duration_callback,
)

requires_redis = pytest.mark.skipif(
    "REDIS_URL" not in os.environ, reason="Requires Redis (REDIS_URL)"
)


def _uid() -> int:
    return uuid.uuid4().int % 10_000_000


# ---------------------------------------------------------------------------
# State-matched caller ID
# ---------------------------------------------------------------------------


class TestStateCallerIdPolicy:
    def test_campaign_setting_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("DOGRAH_STATE_CID_POLICY", "prefer")
        campaign = SimpleNamespace(orchestrator_metadata={"state_cid_policy": "strict"})
        assert resolve_state_cid_policy(campaign) == "strict"

    def test_env_applies_when_campaign_is_silent(self, monkeypatch):
        monkeypatch.setenv("DOGRAH_STATE_CID_POLICY", "prefer")
        assert resolve_state_cid_policy(SimpleNamespace(orchestrator_metadata={})) == "prefer"

    def test_defaults_to_off_and_rejects_junk(self, monkeypatch):
        monkeypatch.delenv("DOGRAH_STATE_CID_POLICY", raising=False)
        assert resolve_state_cid_policy(SimpleNamespace(orchestrator_metadata=None)) == "off"
        campaign = SimpleNamespace(orchestrator_metadata={"state_cid_policy": "maybe"})
        assert resolve_state_cid_policy(campaign) == "off"

    def test_area_code_state_lookup(self):
        assert state_for_number("+18653173943")[0] == "TN"
        assert state_for_number("+12125550100")[0] == "NY"
        assert state_for_number("+18005550100")[0] is None  # toll-free


class TestTransferDestination:
    def test_lead_then_campaign_then_env(self, monkeypatch):
        monkeypatch.setenv("CAMPAIGN_DEFAULT_TRANSFER_DESTINATION", "+15550000003")
        campaign = SimpleNamespace(
            orchestrator_metadata={"transfer_destination": "+15550000002"}
        )
        lead = SimpleNamespace(context_variables={"transfer_destination": "+15550000001"})
        no_lead = SimpleNamespace(context_variables={})
        assert resolve_transfer_destination(lead, campaign) == "+15550000001"
        assert resolve_transfer_destination(no_lead, campaign) == "+15550000002"
        bare = SimpleNamespace(orchestrator_metadata={})
        assert resolve_transfer_destination(no_lead, bare) == "+15550000003"

    def test_none_when_nothing_is_configured(self, monkeypatch):
        monkeypatch.delenv("CAMPAIGN_DEFAULT_TRANSFER_DESTINATION", raising=False)
        assert (
            resolve_transfer_destination(
                SimpleNamespace(context_variables={}),
                SimpleNamespace(orchestrator_metadata=None),
            )
            is None
        )


@pytest.fixture
async def rate_limiter():
    rl = RateLimiter()
    yield rl
    await rl.close()


@requires_redis
class TestAllowedNumbers:
    async def _pool(self, rl, numbers):
        org, config = _uid(), _uid()
        await rl.initialize_from_number_pool(
            org, numbers, telephony_configuration_id=config
        )
        return org, config

    async def test_only_allowed_numbers_are_acquired(self, rate_limiter):
        pool = [f"+1865317{i:04d}" for i in range(3)] + ["+12125550100"]
        org, config = await self._pool(rate_limiter, pool)
        allowed = pool[:2]
        seen = set()
        for _ in range(10):
            n = await rate_limiter.acquire_from_number(
                org, config, allowed_numbers=allowed
            )
            assert n in allowed
            seen.add(n)
            await rate_limiter.release_from_number(
                org, n, telephony_configuration_id=config
            )
        assert seen <= set(allowed)

    async def test_busy_allowed_subset_returns_none(self, rate_limiter):
        pool = ["+18653170001", "+12125550100"]
        org, config = await self._pool(rate_limiter, pool)
        first = await rate_limiter.acquire_from_number(
            org, config, allowed_numbers=["+18653170001"]
        )
        assert first == "+18653170001"
        again = await rate_limiter.acquire_from_number(
            org, config, allowed_numbers=["+18653170001"]
        )
        assert again is None
        # The rest of the pool is still available without the filter.
        assert await rate_limiter.acquire_from_number(org, config) == "+12125550100"

    async def test_empty_allowed_list_acquires_nothing(self, rate_limiter):
        org, config = await self._pool(rate_limiter, ["+18653170001"])
        assert (
            await rate_limiter.acquire_from_number(org, config, allowed_numbers=[])
            is None
        )

    async def test_preferred_ranking_applies_inside_allowed(self, rate_limiter):
        pool = ["+18653170001", "+18653170002", "+12125550100"]
        org, config = await self._pool(rate_limiter, pool)
        n = await rate_limiter.acquire_from_number(
            org,
            config,
            preferred_numbers=["+12125550100", "+18653170002"],
            allowed_numbers=["+18653170001", "+18653170002"],
        )
        # The top preference is outside the allowed subset, so the next one wins.
        assert n == "+18653170002"


# ---------------------------------------------------------------------------
# ARI trunk + transfer caller ID
# ---------------------------------------------------------------------------


class TestAriEndpoint:
    def test_default_trunk_is_appended(self, monkeypatch):
        monkeypatch.setenv("ARI_PJSIP_DEFAULT_TRUNK", "fractel")
        assert _pjsip_endpoint("+18653173943") == "PJSIP/+18653173943@fractel"

    def test_explicit_endpoint_is_kept(self, monkeypatch):
        monkeypatch.setenv("ARI_PJSIP_DEFAULT_TRUNK", "fractel")
        assert _pjsip_endpoint("1001@office") == "PJSIP/1001@office"

    def test_unset_keeps_upstream_behaviour(self, monkeypatch):
        monkeypatch.delenv("ARI_PJSIP_DEFAULT_TRUNK", raising=False)
        assert _pjsip_endpoint("+18653173943") == "PJSIP/+18653173943"


# ---------------------------------------------------------------------------
# Transfer-duration hotfix
# ---------------------------------------------------------------------------


class TestTransferDurationHotfix:
    async def test_max_duration_leaves_a_transferred_call_alone(self):
        engine = MagicMock()
        engine._transfer_handoff_started = True
        engine.end_call_with_reason = AsyncMock()
        await create_max_duration_callback(engine)()
        engine.end_call_with_reason.assert_not_called()

    async def test_max_duration_still_ends_a_normal_call(self):
        engine = MagicMock()
        engine._transfer_handoff_started = False
        engine.end_call_with_reason = AsyncMock()
        await create_max_duration_callback(engine)()
        engine.end_call_with_reason.assert_awaited_once()


# ---------------------------------------------------------------------------
# Fish Audio TTS
# ---------------------------------------------------------------------------


class TestFishTts:
    def test_fish_is_a_registered_tts_provider(self):
        from api.services.configuration.registry import (
            FishAudioTTSConfiguration,
            ServiceProviders,
        )

        config = FishAudioTTSConfiguration(api_key="k", voice="abc")
        assert config.provider == ServiceProviders.FISH
        assert config.latency == "balanced"

    def test_factory_builds_fish_with_the_markup_scrub(self):
        from api.services.pipecat.service_factory import create_tts_service

        user_config = SimpleNamespace(
            tts=SimpleNamespace(
                provider="fish",
                api_key="test-key",
                model="s2.1-pro",
                voice="0123456789abcdef0123456789abcdef",
                latency="balanced",
                speed=None,
                volume=None,
                normalize=True,
            )
        )
        audio_config = SimpleNamespace(
            transport_out_sample_rate=8000, transport_in_sample_rate=8000
        )
        service = create_tts_service(user_config, audio_config)
        assert type(service).__name__ == "FishAudioTTSService"
