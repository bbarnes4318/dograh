"""Inbound routing groups: split inbound calls on a set of numbers across
several agents (workflows) by weight. Changes apply to the next call."""

from fastapi import APIRouter, Depends, HTTPException, Query

from api.db import db_client
from api.db.models import UserModel
from api.schemas.inbound_routing import (
    InboundRoutingAuditEntry,
    InboundRoutingAuditResponse,
    InboundRoutingGroupCreateRequest,
    InboundRoutingGroupListResponse,
    InboundRoutingGroupResponse,
    InboundRoutingGroupUpdateRequest,
    InboundRoutingStatsResponse,
    RoutingConfigNumberSummary,
    RoutingNumberDetachRequest,
    RoutingNumbersChangeResponse,
    RoutingNumberSelection,
    RoutingNumbersSummaryResponse,
)
from api.services.auth.depends import get_user_with_selected_organization
from api.services.telephony.inbound_routing_groups import (
    RoutingGroupValidationError,
    describe_split,
    group_responses,
    group_stats,
    record_audit,
    validate_configurations,
    validate_target_workflows,
)

router = APIRouter(prefix="/inbound-routing-groups", tags=["inbound-routing"])


async def _load_group(group_id: int, organization_id: int):
    group = await db_client.get_inbound_routing_group(group_id, organization_id)
    if not group:
        raise HTTPException(status_code=404, detail="Routing group not found")
    return group


async def _response(group, organization_id: int) -> InboundRoutingGroupResponse:
    return (await group_responses([group], organization_id))[0]


async def _validate_selection(
    selection: RoutingNumberSelection, organization_id: int
) -> None:
    try:
        await validate_configurations(
            selection.telephony_configuration_ids, organization_id
        )
    except RoutingGroupValidationError as e:
        raise HTTPException(status_code=404, detail=str(e))
    if selection.inbound_workflow_id is not None:
        workflow = await db_client.get_workflow(
            selection.inbound_workflow_id, organization_id=organization_id
        )
        if not workflow:
            raise HTTPException(
                status_code=404, detail="Workflow not found in this organization"
            )


@router.get("", response_model=InboundRoutingGroupListResponse)
async def list_inbound_routing_groups(
    user: UserModel = Depends(get_user_with_selected_organization),
):
    org_id = user.selected_organization_id
    groups = await db_client.list_inbound_routing_groups(org_id)
    return InboundRoutingGroupListResponse(groups=await group_responses(groups, org_id))


@router.post("", response_model=InboundRoutingGroupResponse)
async def create_inbound_routing_group(
    request: InboundRoutingGroupCreateRequest,
    user: UserModel = Depends(get_user_with_selected_organization),
):
    org_id = user.selected_organization_id
    try:
        await validate_target_workflows(request.targets, org_id)
    except RoutingGroupValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))

    group = await db_client.create_inbound_routing_group(
        organization_id=org_id,
        name=request.name,
        is_active=request.is_active,
        sticky_by_caller=request.sticky_by_caller,
        targets=[t.model_dump() for t in request.targets],
    )
    await record_audit(
        org_id,
        group.id,
        user.id,
        "group_created",
        {
            "name": group.name,
            "is_active": group.is_active,
            "sticky_by_caller": group.sticky_by_caller,
            "weights": describe_split(group),
        },
    )
    return await _response(group, org_id)


@router.get("/audit", response_model=InboundRoutingAuditResponse)
async def list_inbound_routing_audit(
    group_id: int | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    user: UserModel = Depends(get_user_with_selected_organization),
):
    rows = await db_client.list_inbound_routing_audit(
        user.selected_organization_id, group_id=group_id, limit=limit
    )
    return InboundRoutingAuditResponse(
        entries=[
            InboundRoutingAuditEntry(
                id=r.id,
                group_id=r.group_id,
                user_id=r.user_id,
                user_email=r.user.email if r.user else None,
                action=r.action,
                details=r.details or {},
                created_at=r.created_at,
            )
            for r in rows
        ]
    )


@router.get("/{group_id}", response_model=InboundRoutingGroupResponse)
async def get_inbound_routing_group(
    group_id: int,
    user: UserModel = Depends(get_user_with_selected_organization),
):
    org_id = user.selected_organization_id
    return await _response(await _load_group(group_id, org_id), org_id)


@router.patch("/{group_id}", response_model=InboundRoutingGroupResponse)
async def update_inbound_routing_group(
    group_id: int,
    request: InboundRoutingGroupUpdateRequest,
    user: UserModel = Depends(get_user_with_selected_organization),
):
    org_id = user.selected_organization_id
    before = await _load_group(group_id, org_id)
    before_state = {
        "name": before.name,
        "is_active": before.is_active,
        "sticky_by_caller": before.sticky_by_caller,
        "weights": describe_split(before),
    }
    if request.targets is not None:
        try:
            await validate_target_workflows(request.targets, org_id)
        except RoutingGroupValidationError as e:
            raise HTTPException(status_code=422, detail=str(e))

    group = await db_client.update_inbound_routing_group(
        group_id,
        org_id,
        name=request.name,
        is_active=request.is_active,
        sticky_by_caller=request.sticky_by_caller,
        targets=(
            [t.model_dump() for t in request.targets]
            if request.targets is not None
            else None
        ),
    )
    if not group:
        raise HTTPException(status_code=404, detail="Routing group not found")

    after_state = {
        "name": group.name,
        "is_active": group.is_active,
        "sticky_by_caller": group.sticky_by_caller,
        "weights": describe_split(group),
    }
    changes = {
        key: {"from": before_state[key], "to": after_state[key]}
        for key in before_state
        if before_state[key] != after_state[key]
    }
    if changes:
        await record_audit(
            org_id, group.id, user.id, "group_updated", {"name": group.name, **changes}
        )
    return await _response(group, org_id)


@router.delete("/{group_id}")
async def delete_inbound_routing_group(
    group_id: int,
    user: UserModel = Depends(get_user_with_selected_organization),
):
    org_id = user.selected_organization_id
    group = await _load_group(group_id, org_id)
    counts = await db_client.count_numbers_by_inbound_routing_group(org_id)
    details = {
        "name": group.name,
        "weights": describe_split(group),
        "numbers_released": counts.get(group.id, 0),
    }
    # Audit first: the row's group_id is nulled when the group goes.
    await record_audit(org_id, group.id, user.id, "group_deleted", details)
    await db_client.delete_inbound_routing_group(group_id, org_id)
    return {"deleted": True}


@router.get("/{group_id}/numbers", response_model=RoutingNumbersSummaryResponse)
async def summarize_inbound_routing_group_numbers(
    group_id: int,
    user: UserModel = Depends(get_user_with_selected_organization),
):
    org_id = user.selected_organization_id
    await _load_group(group_id, org_id)
    rows = await db_client.summarize_numbers_for_inbound_routing_group(group_id, org_id)
    return RoutingNumbersSummaryResponse(
        number_count=sum(r["in_this_group"] for r in rows),
        configurations=[RoutingConfigNumberSummary(**r) for r in rows],
    )


@router.post("/{group_id}/numbers", response_model=RoutingNumbersChangeResponse)
async def attach_numbers_to_inbound_routing_group(
    group_id: int,
    request: RoutingNumberSelection,
    user: UserModel = Depends(get_user_with_selected_organization),
):
    """Attach numbers by telephony configuration, explicit ids, or current
    inbound_workflow_id. Numbers in another group move to this one."""
    org_id = user.selected_organization_id
    group = await _load_group(group_id, org_id)
    await _validate_selection(request, org_id)
    affected = await db_client.attach_numbers_to_inbound_routing_group(
        group_id,
        org_id,
        telephony_configuration_ids=request.telephony_configuration_ids,
        phone_number_ids=request.phone_number_ids,
        inbound_workflow_id=request.inbound_workflow_id,
        dry_run=request.dry_run,
    )
    if not request.dry_run and affected:
        await record_audit(
            org_id,
            group.id,
            user.id,
            "numbers_attached",
            {
                "name": group.name,
                "count": affected,
                **request.model_dump(exclude={"dry_run"}),
            },
        )
    counts = await db_client.count_numbers_by_inbound_routing_group(org_id)
    return RoutingNumbersChangeResponse(
        affected=affected, dry_run=request.dry_run, number_count=counts.get(group_id, 0)
    )


@router.post("/{group_id}/numbers/detach", response_model=RoutingNumbersChangeResponse)
async def detach_numbers_from_inbound_routing_group(
    group_id: int,
    request: RoutingNumberDetachRequest,
    user: UserModel = Depends(get_user_with_selected_organization),
):
    """Take numbers out of the group; they fall back to their
    inbound_workflow_id on the next call."""
    org_id = user.selected_organization_id
    group = await _load_group(group_id, org_id)
    if not request.detach_all:
        await _validate_selection(request, org_id)
    affected = await db_client.detach_numbers_from_inbound_routing_group(
        group_id,
        org_id,
        telephony_configuration_ids=request.telephony_configuration_ids,
        phone_number_ids=request.phone_number_ids,
        inbound_workflow_id=request.inbound_workflow_id,
        detach_all=request.detach_all,
        dry_run=request.dry_run,
    )
    if not request.dry_run and affected:
        await record_audit(
            org_id,
            group.id,
            user.id,
            "numbers_detached",
            {
                "name": group.name,
                "count": affected,
                **request.model_dump(exclude={"dry_run"}),
            },
        )
    counts = await db_client.count_numbers_by_inbound_routing_group(org_id)
    return RoutingNumbersChangeResponse(
        affected=affected, dry_run=request.dry_run, number_count=counts.get(group_id, 0)
    )


@router.get("/{group_id}/stats", response_model=InboundRoutingStatsResponse)
async def get_inbound_routing_group_stats(
    group_id: int,
    hours: int = Query(24, ge=1, le=24 * 31),
    user: UserModel = Depends(get_user_with_selected_organization),
):
    org_id = user.selected_organization_id
    group = await _load_group(group_id, org_id)
    return await group_stats(group, org_id, hours)
