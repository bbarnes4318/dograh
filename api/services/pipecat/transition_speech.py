"""Whether node-transition calls may carry a model-written line to speak."""

from api.schemas.workflow_configurations import DEFAULT_SPEAK_DURING_TRANSITION
from api.services.configuration.registry import ServiceProviders


def resolve_speak_during_transition(
    run_configs: dict, *, realtime_provider: str | None
) -> bool:
    """Never under OpenAI Live: its voice model already covers the transition
    gap, and the extra line is spoken as if the workflow wrote it, so the
    agent says things no node asked for ("let me pull that up", "sending you
    the text now" before the tool has run).
    """
    if realtime_provider == ServiceProviders.OPENAI_LIVE.value:
        return False
    return bool(
        run_configs.get("speak_during_transition", DEFAULT_SPEAK_DURING_TRANSITION)
    )
