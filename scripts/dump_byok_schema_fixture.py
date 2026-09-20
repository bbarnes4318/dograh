"""Refresh the BYOK form's provider-schema fixture used by the UI tests.

The BYOK model-configuration form is schema-driven: it renders whatever
``GET /api/v1/user/configurations/defaults`` returns. The fixture keeps the
``llm`` section verbatim (that is what the tests assert against) and, to keep
the file small, only the default provider for the other sections — the form
just needs those keys to exist.

Run from the repo root with the api environment available:

    python -m scripts.dump_byok_schema_fixture
"""

import json
from pathlib import Path

from loguru import logger

logger.remove()

from api.services.configuration.defaults import (  # noqa: E402
    DEFAULT_SERVICE_PROVIDERS,
)
from api.services.configuration.registry import (  # noqa: E402
    REGISTRY,
    ServiceType,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = (
    REPO_ROOT / "ui" / "src" / "components" / "__fixtures__" / "byok-llm-schemas.json"
)

_SECTIONS = {
    "llm": ServiceType.LLM,
    "tts": ServiceType.TTS,
    "stt": ServiceType.STT,
    "embeddings": ServiceType.EMBEDDINGS,
    "realtime": ServiceType.REALTIME,
}


def _provider_key(provider) -> str:
    return str(getattr(provider, "value", provider))


def _section(service_type: ServiceType, only: str | None) -> dict:
    return {
        _provider_key(provider): config_cls.model_json_schema()
        for provider, config_cls in REGISTRY[service_type].items()
        if only is None or _provider_key(provider) == only
    }


def main() -> None:
    default_providers = {
        service: provider.value
        for service, provider in DEFAULT_SERVICE_PROVIDERS.items()
    }
    payload: dict = {}
    for name, service_type in _SECTIONS.items():
        # Keep every LLM provider (the tests assert on the provider list);
        # trim the rest to the default provider.
        only = None if name == "llm" else default_providers.get(name)
        payload[name] = _section(service_type, only)
    payload["default_providers"] = default_providers

    OUTPUT.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"Wrote {OUTPUT.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
