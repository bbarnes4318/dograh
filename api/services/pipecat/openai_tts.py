"""OpenAI TTS with Custom Voice support for the chained pipeline.

Upstream ``OpenAITTSService`` only accepts the named built-in voices as bare
strings. OpenAI's speech endpoint takes custom voices as ``{"id": "voice_..."}``
objects, so this subclass builds the request itself and keeps the voice
selection (built-in vs. custom) separate from pipecat's string-typed settings.

A failing custom voice is surfaced as an explicit error — it is never
swapped for a built-in voice, which would silently change the caller experience.
"""

from typing import Any, AsyncGenerator

from loguru import logger
from openai import APIStatusError

from api.services.configuration.options.openai import (
    CUSTOM_VOICE_UNAVAILABLE_MESSAGE,
    VOICE_TYPE_BUILTIN,
    VOICE_TYPE_CUSTOM,
    openai_voice_param,
)
from pipecat.frames.frames import ErrorFrame, Frame, TTSAudioRawFrame
from pipecat.services.openai.tts import OpenAITTSService
from pipecat.utils.tracing.service_decorators import traced_tts

# Statuses OpenAI returns when a voice id is unknown or not enabled for the project.
_CUSTOM_VOICE_FAILURE_STATUSES = {400, 403, 404}


class DograhOpenAITTSService(OpenAITTSService):
    def __init__(
        self,
        *,
        voice_type: str = VOICE_TYPE_BUILTIN,
        custom_voice_id: str | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._voice_type = voice_type
        self._custom_voice_id = custom_voice_id
        if voice_type == VOICE_TYPE_CUSTOM and not custom_voice_id:
            raise ValueError("custom_voice_id is required for a custom OpenAI voice")

    def build_speech_params(self, text: str) -> dict[str, Any]:
        """Build the ``audio.speech.create`` request body."""
        voice = openai_voice_param(
            self._voice_type, self._settings.voice, self._custom_voice_id
        )
        params: dict[str, Any] = {
            "input": text,
            "model": self._settings.model,
            "voice": voice,
            "response_format": "pcm",
        }
        if self._settings.instructions:
            params["instructions"] = self._settings.instructions
        if self._settings.speed:
            params["speed"] = self._settings.speed
        return params

    @traced_tts
    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        logger.debug(f"{self}: Generating TTS ({len(text)} chars)")
        try:
            params = self.build_speech_params(text)
            async with self._client.audio.speech.with_streaming_response.create(
                **params
            ) as response:
                await self.start_tts_usage_metrics(text)
                async for chunk in response.iter_bytes(self.chunk_size):
                    if chunk:
                        await self.stop_ttfb_metrics()
                        yield TTSAudioRawFrame(
                            chunk, self.sample_rate, 1, context_id=context_id
                        )
        except APIStatusError as e:
            if (
                self._voice_type == VOICE_TYPE_CUSTOM
                and e.status_code in _CUSTOM_VOICE_FAILURE_STATUSES
            ):
                logger.error(
                    f"{self}: OpenAI custom voice rejected "
                    f"(status={e.status_code}, voice_id={self._custom_voice_id})"
                )
                yield ErrorFrame(error=CUSTOM_VOICE_UNAVAILABLE_MESSAGE)
            else:
                logger.error(f"{self}: OpenAI TTS error status={e.status_code}")
                yield ErrorFrame(error=f"OpenAI TTS error (status {e.status_code})")
        except Exception as e:  # noqa: BLE001 - surfaced as a pipeline error frame
            yield ErrorFrame(error=f"OpenAI TTS error: {e}")
