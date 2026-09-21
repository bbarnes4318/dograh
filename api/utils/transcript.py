from typing import List

from pipecat.utils.enums import RealtimeFeedbackType


def _format_timestamp_range(
    payload: dict, event: dict, include_end_timestamps: bool
) -> str:
    start_timestamp = payload.get("timestamp") or event.get("timestamp", "")
    if not include_end_timestamps:
        return start_timestamp

    end_timestamp = payload.get("end_timestamp")
    if end_timestamp:
        return (
            f"{start_timestamp} -> {end_timestamp}"
            if start_timestamp
            else end_timestamp
        )
    return start_timestamp


def generate_searchable_transcript(events: List[dict]) -> str:
    """Transcript text for full-text search: speakers and words, no timestamps.

    The uploaded artifact keeps timestamps because a human reading a call wants
    them. The searchable copy drops them — ISO timestamps tokenize into lexemes
    that bloat the index and never match anything anyone searches for.
    """
    lines: List[str] = []
    for event in events:
        event_type = event.get("type")
        payload = event.get("payload", {})
        if (
            event_type == RealtimeFeedbackType.USER_TRANSCRIPTION.value
            and payload.get("final") is True
        ):
            lines.append(f"user: {payload.get('text', '')}")
        elif event_type == RealtimeFeedbackType.BOT_TEXT.value:
            lines.append(f"assistant: {payload.get('text', '')}")
    return "\n".join(line for line in lines if line.split(": ", 1)[-1].strip())


def generate_transcript_text(
    events: List[dict], *, include_end_timestamps: bool = False
) -> str:
    """Generate transcript text from realtime feedback events.

    Filters for rtf-user-transcription (final) and rtf-bot-text events,
    formats them as '[timestamp] user/assistant: text\\n'. When
    include_end_timestamps is True, formats as
    '[start_timestamp -> end_timestamp] user/assistant: text\\n'.
    """
    lines: List[str] = []
    for event in events:
        event_type = event.get("type")
        payload = event.get("payload", {})

        if (
            event_type == RealtimeFeedbackType.USER_TRANSCRIPTION.value
            and payload.get("final") is True
        ):
            timestamp = _format_timestamp_range(payload, event, include_end_timestamps)
            prefix = f"[{timestamp}] " if timestamp else ""
            lines.append(f"{prefix}user: {payload.get('text', '')}\n")
        elif event_type == RealtimeFeedbackType.BOT_TEXT.value:
            timestamp = _format_timestamp_range(payload, event, include_end_timestamps)
            prefix = f"[{timestamp}] " if timestamp else ""
            lines.append(f"{prefix}assistant: {payload.get('text', '')}\n")

    return "".join(lines)
