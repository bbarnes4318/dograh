"""Model-written transition lines are off under OpenAI Live (see
``resolve_speak_during_transition``): its voice model already covers the gap,
and the extra line made the agent say things no node asked for."""

from api.services.configuration.registry import ServiceProviders
from api.services.pipecat.transition_speech import resolve_speak_during_transition


def test_off_under_openai_live_even_when_configured_on():
    assert not resolve_speak_during_transition(
        {"speak_during_transition": True},
        realtime_provider=ServiceProviders.OPENAI_LIVE.value,
    )


def test_default_on_for_non_realtime_pipelines():
    assert resolve_speak_during_transition({}, realtime_provider=None)


def test_config_can_turn_it_off():
    assert not resolve_speak_during_transition(
        {"speak_during_transition": False}, realtime_provider=None
    )
