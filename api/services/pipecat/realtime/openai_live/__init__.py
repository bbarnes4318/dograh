"""OpenAI GPT-Live (``gpt-live-1``) integration for Dograh.

GPT-Live is the conversational audio layer. The Dograh workflow engine stays
authoritative: node prompts, tools, transitions, SMS, playback and hangup run
in this process through the engine's registered function handlers.
"""

from api.services.pipecat.realtime.openai_live.service import (
    DograhOpenAILiveLLMService,
    build_frontend_instructions,
)

__all__ = ["DograhOpenAILiveLLMService", "build_frontend_instructions"]
