from __future__ import annotations

"""Default provider choices preselected for new service configurations.

The STT default is the platform default, AssemblyAI, unless the operator sets
``STT_PLATFORM_DEFAULT_PROVIDER`` (e.g. to ``deepgram``) to roll it back. This
only changes what a *new* configuration starts with; saved configurations keep
their own provider.
"""


from api.services.configuration.assemblyai import platform_default_stt_provider
from api.services.configuration.registry import (
    REGISTRY,
    ServiceProviders,
    ServiceType,
)

# Previous STT default; used when the env names an unknown provider.
PREVIOUS_DEFAULT_STT_PROVIDER = ServiceProviders.DEEPGRAM


def _default_stt_provider() -> ServiceProviders:
    wanted = platform_default_stt_provider()
    if wanted in REGISTRY[ServiceType.STT] and wanted != ServiceProviders.DOGRAH.value:
        return ServiceProviders(wanted)
    return PREVIOUS_DEFAULT_STT_PROVIDER


# Public mapping of service name -> default provider
DEFAULT_SERVICE_PROVIDERS = {
    "llm": ServiceProviders.OPENAI,
    "tts": ServiceProviders.ELEVENLABS,
    "stt": _default_stt_provider(),
    "embeddings": ServiceProviders.OPENAI,
}

__all__ = [
    "DEFAULT_SERVICE_PROVIDERS",
    "PREVIOUS_DEFAULT_STT_PROVIDER",
]
