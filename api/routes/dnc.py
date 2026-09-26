"""Manage an organization's do-not-call suppression list."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api.db import db_client
from api.db.models import UserModel
from api.services.auth.depends import get_user
from api.services.dnc import (
    SOURCE_IMPORT,
    SOURCE_MANUAL,
    dnc_service,
    normalize_dnc_number,
)

router = APIRouter(prefix="/dnc")

# A bulk paste from a spreadsheet, not a file upload. Larger lists belong in a
# background import rather than one request the client waits on.
MAX_NUMBERS_PER_REQUEST = 5000


class DNCEntryResponse(BaseModel):
    id: int
    phone_number: str
    raw_input: str | None = None
    source: str
    reason: str | None = None
    expires_at: datetime | None = None
    workflow_run_id: int | None = None
    created_at: datetime


class DNCListResponse(BaseModel):
    entries: list[DNCEntryResponse]
    total: int


class AddNumbersRequest(BaseModel):
    phone_numbers: list[str] = Field(
        ..., min_length=1, max_length=MAX_NUMBERS_PER_REQUEST
    )
    reason: str | None = Field(default=None, max_length=500)
    # Null means the suppression never lapses.
    expires_at: datetime | None = None


class AddNumbersResponse(BaseModel):
    added: int
    already_listed: int
    invalid: list[str]


class CheckNumberResponse(BaseModel):
    phone_number: str
    normalized: str | None
    suppressed: bool


def _to_response(entry) -> DNCEntryResponse:
    return DNCEntryResponse(
        id=entry.id,
        phone_number=entry.phone_number,
        raw_input=entry.raw_input,
        source=entry.source,
        reason=entry.reason,
        expires_at=entry.expires_at,
        workflow_run_id=entry.workflow_run_id,
        created_at=entry.created_at,
    )


@router.get("/")
async def list_dnc_entries(
    search: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    user: UserModel = Depends(get_user),
) -> DNCListResponse:
    """List suppressed numbers for the authenticated user's organization."""
    organization_id = user.selected_organization_id
    entries = await db_client.list_dnc_entries(
        organization_id=organization_id,
        search=search,
        limit=limit,
        offset=offset,
    )
    total = await db_client.count_dnc_entries(
        organization_id=organization_id, search=search
    )
    return DNCListResponse(
        entries=[_to_response(entry) for entry in entries], total=total
    )


@router.post("/")
async def add_dnc_entries(
    request: AddNumbersRequest,
    user: UserModel = Depends(get_user),
) -> AddNumbersResponse:
    """Suppress one or more numbers.

    Rows that aren't usable phone numbers come back in ``invalid`` rather than
    failing the request, so a paste of ten thousand numbers isn't rejected
    because one cell holds a note.
    """
    result = await dnc_service.add_numbers(
        organization_id=user.selected_organization_id,
        raw_numbers=request.phone_numbers,
        source=SOURCE_IMPORT if len(request.phone_numbers) > 1 else SOURCE_MANUAL,
        reason=request.reason,
        created_by=user.id,
        expires_at=request.expires_at,
    )
    return AddNumbersResponse(
        added=result.added,
        already_listed=result.already_listed,
        invalid=result.invalid,
    )


@router.get("/check")
async def check_number(
    phone_number: str = Query(..., min_length=1, max_length=64),
    user: UserModel = Depends(get_user),
) -> CheckNumberResponse:
    """Whether one number is currently suppressed.

    Returns the canonical form it was matched under, so a caller can see why a
    number they expected to match didn't.
    """
    suppressed = await dnc_service.is_suppressed(
        organization_id=user.selected_organization_id, raw_number=phone_number
    )
    return CheckNumberResponse(
        phone_number=phone_number,
        normalized=normalize_dnc_number(phone_number),
        suppressed=suppressed,
    )


@router.delete("/{phone_number:path}")
async def remove_dnc_entry(
    phone_number: str,
    user: UserModel = Depends(get_user),
) -> dict:
    """Remove a number from the suppression list."""
    removed = await dnc_service.remove_number(
        organization_id=user.selected_organization_id, raw_number=phone_number
    )
    if not removed:
        raise HTTPException(
            status_code=404, detail="Number is not on the do-not-call list"
        )
    return {"removed": True}
