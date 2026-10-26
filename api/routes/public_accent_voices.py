"""Public preview audio for the local-accent Fish voices.

No auth: previews are short designed-voice samples of a fixed script, not
customer data, and the voice picker plays them straight from an <audio> tag.
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from api.services.voice_routing.accent_voices import (
    PREVIEW_FILENAME_RE,
    get_catalog,
)

router = APIRouter(prefix="/public/accent-voices")

_MEDIA_TYPES = {"mp3": "audio/mpeg", "wav": "audio/wav"}


@router.get("/previews/{filename}")
async def get_accent_voice_preview(filename: str):
    """Serve ``NN_<gender>.mp3|wav`` from ``api/data/accent_voices/previews``."""
    if not PREVIEW_FILENAME_RE.match(filename):
        raise HTTPException(status_code=404, detail="Preview not found")

    path = get_catalog().previews_dir / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Preview not found")

    return FileResponse(
        path,
        media_type=_MEDIA_TYPES[filename.rsplit(".", 1)[1]],
        headers={"Cache-Control": "public, max-age=86400"},
    )
