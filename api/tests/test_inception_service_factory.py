"""Inception (Mercury) BYOK provider: config, factory wiring, and guards."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from api.schemas.ai_model_configuration import EffectiveAIModelConfiguration
from api.services.configuration.registry import (
    INCEPTION_DEFAULT_BASE_URL,
    INCEPTION_DEFAULT_MODEL,
    INCEPTION_DEFAULT_REASONING_EFFORT,
    REGISTRY,
    InceptionLLMConfiguration,
    ServiceProviders,
    ServiceType,
)
from api.services.configuration.resolve import resolve_effective_config
from api.services.pipecat.service_factory import (
    DograhInceptionLLMService,
    create_llm_service,
    create_llm_service_from_provider,
)


def _is_dograh_inception(service) -> bool:
    """Identify the service by class name rather than ``isinstance``.

    Under pytest's importlib import mode the factory module can be imported
    under more than one module identity across the suite, which makes the
    class objects compare unequal even though they are the same source class.
    """
    return type(service).__name__ == DograhInceptionLLMService.__name__


class TestInceptionLLMConfiguration:
    def test_registered_for_llm(self):
        assert REGISTRY[ServiceType.LLM][ServiceProviders.INCEPTION] is (
            InceptionLLMConfiguration
        )

    def test_default_values(self):
        config = InceptionLLMConfiguration(api_key="test-key")
        assert config.provider == ServiceProviders.INCEPTION
        assert config.model == "mercury-2.5" == INCEPTION_DEFAULT_MODEL
        assert config.base_url == "https://api.inceptionlabs.ai/v1"
        assert config.base_url == INCEPTION_DEFAULT_BASE_URL
        assert config.reasoning_effort == "low" == INCEPTION_DEFAULT_REASONING_EFFORT

    def test_reasoning_effort_enum_is_exposed_to_the_ui(self):
        """The BYOK form renders a select from the JSON schema's enum."""
        schema = InceptionLLMConfiguration.model_json_schema()
        field = schema["properties"]["reasoning_effort"]
        assert field["enum"] == ["instant", "low", "medium", "high"]
        assert field["default"] == "low"
        assert schema["properties"]["model"]["default"] == "mercury-2.5"
        assert schema["properties"]["base_url"]["default"] == (
            "https://api.inceptionlabs.ai/v1"
        )

    def test_reasoning_effort_rejects_unknown_value(self):
        with pytest.raises(ValueError):
            InceptionLLMConfiguration(api_key="k", reasoning_effort="turbo")


class TestInceptionLLMServiceFactory:
    def test_create_from_provider_passes_model_and_reasoning_effort(self):
        service = create_llm_service_from_provider(
            provider=ServiceProviders.INCEPTION.value,
            model="mercury-2.5",
            api_key="test-key",
            reasoning_effort="high",
        )
        assert _is_dograh_inception(service)
        assert service._settings.model == "mercury-2.5"
        assert service._settings.reasoning_effort == "high"
        assert str(service._client.base_url).rstrip("/") == INCEPTION_DEFAULT_BASE_URL

    def test_create_from_provider_defaults_reasoning_effort(self):
        """Call sites that don't know about the knob still get the saved default."""
        service = create_llm_service_from_provider(
            provider=ServiceProviders.INCEPTION.value,
            model="mercury-2.5",
            api_key="test-key",
        )
        assert service._settings.reasoning_effort == INCEPTION_DEFAULT_REASONING_EFFORT

    def test_create_from_provider_honours_custom_base_url(self):
        service = create_llm_service_from_provider(
            provider=ServiceProviders.INCEPTION.value,
            model="mercury-2.5",
            api_key="test-key",
            base_url="https://gateway.example.com/v1",
        )
        assert str(service._client.base_url).rstrip("/") == (
            "https://gateway.example.com/v1"
        )

    def test_create_from_provider_rejects_internal_base_url(self, monkeypatch):
        """Same SSRF guard the other OpenAI-compatible providers get."""
        monkeypatch.setattr("api.utils.url_security.DEPLOYMENT_MODE", "saas")

        with pytest.raises(HTTPException) as exc_info:
            create_llm_service_from_provider(
                provider=ServiceProviders.INCEPTION.value,
                model="mercury-2.5",
                api_key="test-key",
                base_url="http://169.254.169.254/latest",
            )

        assert exc_info.value.status_code == 400

    def test_create_llm_service_extracts_reasoning_effort_from_org_config(self):
        """Organization-level config path (pipeline runner, text chat, voicemail)."""
        user_config = SimpleNamespace(
            llm=InceptionLLMConfiguration(
                api_key="test-key", model="mercury-2.5", reasoning_effort="medium"
            )
        )
        service = create_llm_service(user_config)
        assert service._settings.model == "mercury-2.5"
        assert service._settings.reasoning_effort == "medium"


class TestInceptionModelOverrides:
    def test_agent_level_override_carries_reasoning_effort(self):
        """Agent -> Settings -> Model Overrides must carry the field too."""
        org_config = EffectiveAIModelConfiguration(
            llm={
                "provider": ServiceProviders.OPENAI.value,
                "model": "gpt-4.1",
                "api_key": "openai-key",
            }
        )
        effective = resolve_effective_config(
            org_config,
            {
                "llm": {
                    "provider": ServiceProviders.INCEPTION.value,
                    "model": "mercury-2.5",
                    "api_key": "inception-key",
                    "reasoning_effort": "instant",
                }
            },
        )
        assert isinstance(effective.llm, InceptionLLMConfiguration)
        assert effective.llm.reasoning_effort == "instant"

        service = create_llm_service(effective)
        assert _is_dograh_inception(service)
        assert service._settings.reasoning_effort == "instant"

        # The org-level config is untouched by the override.
        assert org_config.llm.provider == ServiceProviders.OPENAI
        assert org_config.llm.model == "gpt-4.1"


class TestInceptionQAPath:
    """QA analysis / node summaries resolve config independently of the factory."""

    async def test_resolve_user_llm_config_carries_inception_fields(self):
        from api.services.workflow.qa import llm_config as qa_llm_config

        user_configuration = EffectiveAIModelConfiguration(
            llm={
                "provider": ServiceProviders.INCEPTION.value,
                "model": "mercury-2.5",
                "api_key": "inception-key",
                "reasoning_effort": "medium",
            }
        )
        workflow_run = SimpleNamespace(
            workflow=SimpleNamespace(organization_id=1, workflow_configurations={}),
            definition=None,
        )

        async def fake_effective(*_args, **_kwargs):
            return user_configuration

        with patch(
            "api.services.configuration.ai_model_configuration."
            "get_effective_ai_model_configuration_for_workflow",
            new=fake_effective,
        ):
            (
                provider,
                model,
                api_key,
                kwargs,
            ) = await qa_llm_config.resolve_user_llm_config(workflow_run)

        assert provider == ServiceProviders.INCEPTION.value
        assert model == "mercury-2.5"
        assert api_key == "inception-key"
        assert kwargs["reasoning_effort"] == "medium"
        assert kwargs["base_url"] == INCEPTION_DEFAULT_BASE_URL

        # These kwargs are spread straight into the factory by QA analysis and
        # node summaries, so the effort actually reaches the service.
        service = create_llm_service_from_provider(provider, model, api_key, **kwargs)
        assert _is_dograh_inception(service)
        assert service._settings.reasoning_effort == "medium"


class TestInceptionStructuredOutputGuard:
    def _params(self, service, extra_settings=None):
        if extra_settings:
            service._settings.extra.update(extra_settings)
        invocation = {
            "messages": [{"role": "user", "content": "return json please"}],
        }
        return service.build_chat_completion_params(invocation)

    def test_json_schema_response_format_is_downgraded_to_json_mode(self):
        service = create_llm_service_from_provider(
            provider=ServiceProviders.INCEPTION.value,
            model="mercury-2.5",
            api_key="test-key",
        )
        params = self._params(
            service,
            {
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "vars", "strict": True, "schema": {}},
                }
            },
        )
        assert params["response_format"] == {"type": "json_object"}

    def test_json_object_response_format_is_left_alone(self):
        service = create_llm_service_from_provider(
            provider=ServiceProviders.INCEPTION.value,
            model="mercury-2.5",
            api_key="test-key",
        )
        params = self._params(service, {"response_format": {"type": "json_object"}})
        assert params["response_format"] == {"type": "json_object"}

    def test_other_providers_keep_json_schema_response_format(self):
        """The guard must not change behaviour for any non-Inception provider."""
        service = create_llm_service_from_provider(
            provider=ServiceProviders.OPENAI.value,
            model="gpt-4.1",
            api_key="test-key",
        )
        response_format = {
            "type": "json_schema",
            "json_schema": {"name": "vars", "strict": True, "schema": {}},
        }
        params = self._params(service, {"response_format": response_format})
        assert params["response_format"] == response_format

    def test_request_body_carries_model_and_reasoning_effort(self):
        service = create_llm_service_from_provider(
            provider=ServiceProviders.INCEPTION.value,
            model="mercury-2.5",
            api_key="test-key",
            reasoning_effort="low",
        )
        params = self._params(service)
        assert params["model"] == "mercury-2.5"
        assert params["reasoning_effort"] == "low"
        # `realtime` is Inception-only and unknown to the OpenAI SDK, so
        # pipecat smuggles it through extra_body rather than a top-level key.
        assert params["extra_body"] == {"realtime": True}


class TestNonOverriddenAgentUnaffected:
    def test_agent_without_an_override_keeps_the_org_provider(self):
        org_config = EffectiveAIModelConfiguration(
            llm={
                "provider": ServiceProviders.OPENAI.value,
                "model": "gpt-4.1",
                "api_key": "openai-key",
            }
        )
        tts_only_override = {"tts": {"provider": "elevenlabs", "api_key": "eleven-key"}}
        for overrides in (None, {}, tts_only_override):
            effective = resolve_effective_config(org_config, overrides)
            assert effective.llm.provider == ServiceProviders.OPENAI
            assert effective.llm.model == "gpt-4.1"
            service = create_llm_service(effective)
            assert type(service).__name__ == "OpenAILLMService"


class TestInceptionAPIKeyValidation:
    """Unregistered providers are rejected outright at save time, so Inception
    needs its own validator entry."""

    def _validator(self):
        from api.services.configuration.check_validity import (
            UserConfigurationValidator,
        )

        return UserConfigurationValidator()

    def test_provider_has_a_validator(self):
        validator = self._validator()
        assert ServiceProviders.INCEPTION.value in validator._validator_map

    def test_accepts_a_key_the_api_does_not_reject(self):
        config = InceptionLLMConfiguration(api_key="sk_live")
        with patch("api.services.configuration.check_validity.httpx.get") as mock_get:
            mock_get.return_value = SimpleNamespace(status_code=200)
            assert self._validator()._validate_service(config, "llm") == []

        called_url = mock_get.call_args.args[0]
        assert called_url == "https://api.inceptionlabs.ai/v1/models"
        assert mock_get.call_args.kwargs["headers"]["Authorization"] == "Bearer sk_live"

    def test_rejects_a_key_the_api_refuses(self):
        config = InceptionLLMConfiguration(api_key="sk_bad")
        with patch("api.services.configuration.check_validity.httpx.get") as mock_get:
            mock_get.return_value = SimpleNamespace(status_code=401)
            statuses = self._validator()._validate_service(config, "llm")

        assert len(statuses) == 1
        assert "Invalid Inception API key" in statuses[0]["message"]

    def test_checks_a_custom_base_url_instead_of_the_default(self):
        config = InceptionLLMConfiguration(
            api_key="sk_live", base_url="https://gateway.example.com/v1"
        )
        with patch("api.services.configuration.check_validity.httpx.get") as mock_get:
            mock_get.return_value = SimpleNamespace(status_code=200)
            assert self._validator()._validate_service(config, "llm") == []

        assert mock_get.call_args.args[0] == "https://gateway.example.com/v1/models"

    def test_upstream_outage_does_not_block_saving(self):
        config = InceptionLLMConfiguration(api_key="sk_live")
        with patch("api.services.configuration.check_validity.httpx.get") as mock_get:
            mock_get.return_value = SimpleNamespace(status_code=503)
            assert self._validator()._validate_service(config, "llm") == []
