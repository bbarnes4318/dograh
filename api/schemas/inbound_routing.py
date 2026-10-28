"""Request/response types for inbound routing groups."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RoutingTarget(BaseModel):
    workflow_id: int
    weight: int = Field(..., ge=0, le=100)


def _validate_targets(targets: List[RoutingTarget]) -> List[RoutingTarget]:
    ids = [t.workflow_id for t in targets]
    if len(ids) != len(set(ids)):
        raise ValueError("Each agent can only appear once in a routing group")
    total = sum(t.weight for t in targets if t.weight > 0)
    if total != 100:
        raise ValueError(f"Agent weights must add up to exactly 100 (got {total})")
    return targets


class InboundRoutingGroupCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    is_active: bool = True
    sticky_by_caller: bool = True
    targets: List[RoutingTarget] = Field(..., min_length=1)

    @field_validator("name")
    @classmethod
    def strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Name cannot be empty")
        return v

    @field_validator("targets")
    @classmethod
    def check_targets(cls, v: List[RoutingTarget]) -> List[RoutingTarget]:
        return _validate_targets(v)


class InboundRoutingGroupUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    is_active: Optional[bool] = None
    sticky_by_caller: Optional[bool] = None
    targets: Optional[List[RoutingTarget]] = None

    @field_validator("name")
    @classmethod
    def strip_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("Name cannot be empty")
        return v

    @field_validator("targets")
    @classmethod
    def check_targets(
        cls, v: Optional[List[RoutingTarget]]
    ) -> Optional[List[RoutingTarget]]:
        if v is None:
            return v
        if not v:
            raise ValueError("A routing group needs at least one agent")
        return _validate_targets(v)


class RoutingTargetResponse(BaseModel):
    workflow_id: int
    workflow_name: Optional[str] = None
    weight: int


class InboundRoutingGroupResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    is_active: bool
    sticky_by_caller: bool
    number_count: int = 0
    targets: List[RoutingTargetResponse]
    created_at: datetime
    updated_at: datetime


class InboundRoutingGroupListResponse(BaseModel):
    groups: List[InboundRoutingGroupResponse]


class RoutingNumberSelection(BaseModel):
    """Numbers matching ANY selector are affected. Always org-scoped."""

    telephony_configuration_ids: List[int] = Field(default_factory=list)
    phone_number_ids: List[int] = Field(default_factory=list)
    # "Every number whose inbound_workflow_id is X".
    inbound_workflow_id: Optional[int] = None
    # Count only; change nothing. Used for the UI's confirmation prompt.
    dry_run: bool = False


class RoutingNumberDetachRequest(RoutingNumberSelection):
    detach_all: bool = False


class RoutingNumbersChangeResponse(BaseModel):
    affected: int
    dry_run: bool
    number_count: int


class RoutingConfigNumberSummary(BaseModel):
    telephony_configuration_id: int
    name: str
    provider: str
    total_numbers: int
    in_this_group: int
    in_other_group: int


class RoutingNumbersSummaryResponse(BaseModel):
    number_count: int
    configurations: List[RoutingConfigNumberSummary]


class RoutingTargetStats(BaseModel):
    workflow_id: int
    workflow_name: Optional[str] = None
    calls: int
    actual_percent: float
    target_percent: int
    # A workflow that took calls but isn't (or is no longer) a target, e.g.
    # the inbound_workflow_id fallback or a target removed mid-window.
    is_target: bool = True


class InboundRoutingStatsResponse(BaseModel):
    group_id: int
    hours: int
    since: datetime
    total_calls: int
    targets: List[RoutingTargetStats]


class InboundRoutingAuditEntry(BaseModel):
    id: int
    group_id: Optional[int] = None
    user_id: Optional[int] = None
    user_email: Optional[str] = None
    action: str
    details: Dict[str, Any]
    created_at: datetime


class InboundRoutingAuditResponse(BaseModel):
    entries: List[InboundRoutingAuditEntry]
