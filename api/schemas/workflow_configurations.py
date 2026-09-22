from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_MAX_CALL_DURATION_SECONDS = 300
# Hard ceiling on configurable call duration. Must stay <= the concurrency
# rate limiter's stale_call_timeout (20 min): a call running past that has
# its slot purged as stale and the org concurrency limit under-counts.
MAX_CALL_DURATION_SECONDS = 1200
DEFAULT_MAX_USER_IDLE_TIMEOUT_SECONDS = 10.0
DEFAULT_SMART_TURN_STOP_SECS = 2.0
DEFAULT_TURN_START_STRATEGY = "default"
DEFAULT_TURN_START_MIN_WORDS = 3
DEFAULT_PROVISIONAL_VAD_PAUSE_SECS = 1.5
DEFAULT_TURN_STOP_STRATEGY = "transcription"
DEFAULT_CONTEXT_COMPACTION_ENABLED = False
DEFAULT_BUFFER_MUTED_SPEECH = True
DEFAULT_SPEAK_DURING_TRANSITION = True


class ExternalPBXFieldMapping(BaseModel):
    """Map one gathered-context value to a provider-native field."""

    context_path: str = Field(min_length=1, max_length=255)
    destination_field: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")

    @field_validator("context_path", mode="before")
    @classmethod
    def strip_context_path(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("destination_field", mode="before")
    @classmethod
    def strip_destination_field(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class AmbientNoiseConfigurationDefaults(BaseModel):
    model_config = ConfigDict(extra="allow")

    enabled: bool = False
    volume: float = 0.3


class ToolFillerConfiguration(BaseModel):
    """Short holding phrase spoken when a tool call outruns its deadline.

    Tools can already be configured to speak a message *before* they run, but
    that fires even when the call returns in 50ms — so the agent sounds slow
    on fast tools and silent on slow ones. This speaks only once a call has
    actually taken too long, and covers every tool (HTTP, MCP) rather than
    only the ones someone remembered to configure.
    """

    model_config = ConfigDict(extra="allow")

    enabled: bool = True
    # Below roughly a second, callers read a pause as normal conversational
    # rhythm rather than dead air.
    delay_seconds: float = Field(default=1.2, gt=0, le=10)
    phrases: list[str] = Field(
        default_factory=lambda: [
            "One moment.",
            "Let me check that.",
            "Just a second.",
        ],
        max_length=20,
    )


class DTMFConfiguration(BaseModel):
    """Keypad handling for telephony calls.

    Capture is on by default and costs nothing: a caller who presses keys
    instead of speaking is currently ignored entirely.

    Sending is opt-in because it puts a tool on every node — worth it for
    outbound calls into business phone menus, where an agent that can't press
    "2 for sales" never reaches a human, and noise for consumer calls.
    """

    model_config = ConfigDict(extra="allow")

    capture_enabled: bool = True
    send_enabled: bool = False
    # Quiet after a keypress before the entry is treated as finished.
    interdigit_timeout_seconds: float = Field(default=2.5, gt=0, le=30)
    max_digits: int = Field(default=32, gt=0, le=64)


class IdleNudgeConfiguration(BaseModel):
    """One step in the escalating response to a caller going quiet."""

    model_config = ConfigDict(extra="allow")

    # Seconds of silence before *this* step fires. None keeps the previous
    # step's timeout (the first step always uses `max_user_idle_timeout`).
    after_seconds: Optional[float] = Field(default=None, gt=0, le=120)
    # A canned line, spoken straight to TTS with no LLM round trip — the point
    # is to be instant at the moment the caller is already disengaging. Set to
    # null to have the LLM generate the nudge instead, which is what a
    # non-English workflow wants so the nudge matches the caller's language.
    message: Optional[str] = None
    # Instruction handed to the LLM when `message` is null.
    llm_instruction: Optional[str] = None
    end_call: bool = False


def default_idle_nudges() -> list["IdleNudgeConfiguration"]:
    return [
        IdleNudgeConfiguration(message="Sorry, are you still there?"),
        IdleNudgeConfiguration(
            after_seconds=8.0,
            llm_instruction=(
                "The user has gone quiet again. Briefly re-ask your last "
                "question in a different way, in the language the user has "
                "been speaking."
            ),
        ),
        IdleNudgeConfiguration(
            after_seconds=8.0,
            llm_instruction=(
                "The user has been quiet. We will be disconnecting the call "
                "now. Wish them a good day in the language that the user has "
                "been speaking so far."
            ),
            end_call=True,
        ),
    ]


class IdleBehaviorConfiguration(BaseModel):
    """How the agent escalates when the caller stops responding.

    The default gives the caller one more chance than a straight
    nudge-then-hang-up, and makes the first nudge instant instead of waiting on
    a model round trip at exactly the moment attention is slipping.
    """

    model_config = ConfigDict(extra="allow")

    enabled: bool = True
    nudges: list[IdleNudgeConfiguration] = Field(
        default_factory=default_idle_nudges, max_length=10
    )


class NoiseSuppressionConfiguration(BaseModel):
    """RNNoise suppression on inbound caller audio.

    Off by default. RNNoise runs at 48 kHz, so narrowband telephony audio is
    resampled up and back down, which costs CPU and adds roughly 20ms to the
    inbound path. On a quiet line it changes almost nothing; measured against
    recorded speech it recovers ~9dB SNR at 8 kHz and ~10dB at 16 kHz once the
    background is loud enough to matter. Turn it on for consumer outbound,
    where callers are in cars and shops, and leave it off for clean lines.
    """

    model_config = ConfigDict(extra="allow")

    enabled: bool = False
    # Quick by default: this sits in a live call's inbound path, so latency
    # beats fidelity on a signal that is about to be denoised anyway.
    resampler_quality: Literal["VHQ", "HQ", "MQ", "LQ", "QQ"] = "QQ"


class DNCConfiguration(BaseModel):
    """Do-not-call suppression.

    Numbers on the list are skipped at dial time, so a number added while a
    campaign is running stops the calls still queued against it.
    """

    model_config = ConfigDict(extra="allow")

    # On by default: a caller asking not to be contacted again is a request
    # the platform should honour without anyone opting in to honouring it.
    # Unlike the DTMF sender this adds a tool schema to every node, which is
    # the price of hearing the request at all.
    #
    # A call ending in a DNC disposition always suppresses the number; that
    # isn't configurable, because a workflow that classifies a caller as DNC
    # and then calls them again is not a setting anyone wants.
    agent_tool_enabled: bool = True


class WorkflowConfigurationDefaults(BaseModel):
    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _treat_null_as_unset(cls, data):
        # Stored configs (and older clients) carry explicit JSON nulls for
        # keys the user never configured; dropping them lets the field
        # defaults apply instead of failing validation.
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if v is not None}
        return data

    ambient_noise_configuration: AmbientNoiseConfigurationDefaults = Field(
        default_factory=AmbientNoiseConfigurationDefaults
    )
    noise_suppression: NoiseSuppressionConfiguration = Field(
        default_factory=NoiseSuppressionConfiguration
    )
    tool_filler: ToolFillerConfiguration = Field(
        default_factory=ToolFillerConfiguration
    )
    idle_behavior: IdleBehaviorConfiguration = Field(
        default_factory=IdleBehaviorConfiguration
    )
    dtmf: DTMFConfiguration = Field(default_factory=DTMFConfiguration)
    dnc: DNCConfiguration = Field(default_factory=DNCConfiguration)
    max_call_duration: int = Field(
        default=DEFAULT_MAX_CALL_DURATION_SECONDS,
        gt=0,
        le=MAX_CALL_DURATION_SECONDS,
    )
    max_user_idle_timeout: float = DEFAULT_MAX_USER_IDLE_TIMEOUT_SECONDS
    smart_turn_stop_secs: float = DEFAULT_SMART_TURN_STOP_SECS
    turn_start_strategy: Literal["default", "min_words", "provisional_vad"] = (
        DEFAULT_TURN_START_STRATEGY
    )
    turn_start_min_words: int = DEFAULT_TURN_START_MIN_WORDS
    provisional_vad_pause_secs: float = DEFAULT_PROVISIONAL_VAD_PAUSE_SECS
    turn_stop_strategy: Literal["transcription", "turn_analyzer"] = (
        DEFAULT_TURN_STOP_STRATEGY
    )
    dictionary: str = ""
    context_compaction_enabled: bool = DEFAULT_CONTEXT_COMPACTION_ENABLED
    # When the caller is muted (a no-interrupt node, a transition line, a tool
    # call), hold what they said and replay it into the context once they're
    # unmuted instead of dropping it.
    buffer_muted_speech: bool = DEFAULT_BUFFER_MUTED_SPEECH
    # Let the model hand back a short line to speak while a node transition
    # runs, covering the gap between the transition call and the generation
    # that speaks in the new node. Edges with their own configured transition
    # speech are unaffected.
    speak_during_transition: bool = DEFAULT_SPEAK_DURING_TRANSITION
    external_pbx_field_mappings: list[ExternalPBXFieldMapping] = Field(
        default_factory=list,
        max_length=100,
    )


def get_default_workflow_configurations() -> WorkflowConfigurationDefaults:
    return WorkflowConfigurationDefaults()
