"""Public preview audio for the local-accent Fish clones.

No auth: each preview is a short sample of a fixed script synthesized from the
accent's Fish clone, not customer data, and the voice picker plays it straight
from an <audio> tag. Only ``NNN.mp3`` files in ``previews/`` are served; the
reference recordings the clones were made from are never reachable.
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from api.services.voice_routing.accent_voices import (
    PREVIEW_FILENAME_RE,
    get_catalog,
)

router = APIRouter(prefix="/public/accent-voices")


@router.get("/previews/{filename}")
async def get_accent_voice_preview(filename: str):
    """Serve ``NNN.mp3`` from ``api/data/accent_voices/previews``."""
    # The strict pattern (digits + ".mp3") rules out path separators and "..".
    if not PREVIEW_FILENAME_RE.fullmatch(filename):
        raise HTTPException(status_code=404, detail="Preview not found")

    path = get_catalog().previews_dir / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Preview not found")

    return FileResponse(
        path,
        media_type="audio/mpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )
