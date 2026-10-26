from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException

from api.db.models import UserModel
from api.services.auth.depends import get_user

from .installer import HoppwhistleInstallRequest, InstallError, install_hoppwhistle

router = APIRouter(prefix="/integrations/hoppwhistle", tags=["integrations"])


@router.post("/install")
async def install(
    request: HoppwhistleInstallRequest,
    user: UserModel = Depends(get_user),
) -> dict:
    """Install (or refresh) the Hoppwhistle tools in the caller's organization."""
    if not user.selected_organization_id:
        raise HTTPException(
            status_code=400, detail="No organization selected for the user"
        )
    try:
        result = await install_hoppwhistle(
            request,
            organization_id=int(user.selected_organization_id),
            user_id=int(user.id),
        )
    except InstallError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return asdict(result)
