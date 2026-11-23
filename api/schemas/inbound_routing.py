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
    # The group's updated_at as the editor loaded it. When given, the save is
    # refused (409) if someone else saved the group in the meantime.
    expected_updated_at: Optional[datetime] = None

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
    # "Agent (Workspace label)" for a linked workspace's agent; kept for
    # older clients. New clients show agent_name and organization_label.
    workflow_name: Optional[str] = None
    weight: int
    agent_name: Optional[str] = None
    # The workspace the agent runs in (its calls, transcripts and recordings
    # are stored there) and its label.
    organization_id: Optional[int] = None
    organization_label: Optional[str] = None
    is_linked_workspace: bool = False


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
    agent_name: Optional[str] = None
    organization_id: Optional[int] = None
    organization_label: Optional[str] = None
    # Times the weights picked this agent first (sticky or random), whether or
    # not it then took the call.
    picked: int = 0
    # Times it was picked or tried but couldn't take the call, and why.
    unavailable: int = 0
    unavailable_reasons: Dict[str, int] = Field(default_factory=dict)
    # Calls it took only because the agent picked first couldn't.
    fallback_calls: int = 0


class RoutingRefusal(BaseModel):
    reason: str
    organization_id: Optional[int] = None
    workflow_id: Optional[int] = None
    count: int


class InboundRoutingStatsResponse(BaseModel):
    group_id: int
    hours: int
    since: datetime
    # Calls that reached an agent (inbound runs this group routed).
    total_calls: int
    targets: List[RoutingTargetStats]
    # Calls routed by this group in the window: answered + refused after the
    # routing decision (concurrency limit, quota, workspace unavailable).
    eligible_calls: int = 0
    # Calls that did not go to the agent picked first (another target or the
    # number's own agent took it), with why.
    fallback_calls: int = 0
    fallback_reasons: Dict[str, int] = Field(default_factory=dict)
    # Fallbacks that ended in a different workspace (business) than picked.
    cross_workspace_fallbacks: int = 0
    refused_calls: int = 0
    refusals: List[RoutingRefusal] = Field(default_factory=list)
    sticky_by_caller: bool = False
    # Plain-language caution about reading percentages from few calls.
    sample_note: Optional[str] = None


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


class LinkedAgent(BaseModel):
    workflow_id: int
    name: str


class LinkedWorkspaceAgents(BaseModel):
    """A workspace this org has linked, whose agents its routing groups can
    send calls to. Those calls show up in that workspace."""

    organization_id: int
    label: str
    agents: List[LinkedAgent]


class LinkedWorkspaceAgentsResponse(BaseModel):
    workspaces: List[LinkedWorkspaceAgents]
    # This workspace's own label (operator-set ``self_label``), shown on its
    # own agents next to the linked ones, e.g. "Alex — Final Expense".
    self_label: Optional[str] = None


class SharedWithWorkspace(BaseModel):
    organization_id: int
    label: str
    # The linked workspace's agents' combined share of the number's calls.
    share_percent: float


class OwnedNumber(BaseModel):
    phone_number_id: int
    address: str
    label: Optional[str] = None
    is_active: bool
    telephony_configuration_id: int
    telephony_configuration_name: Optional[str] = None
    provider: Optional[str] = None
    inbound_routing_group_id: Optional[int] = None
    routing_group_name: Optional[str] = None
    routing_group_active: Optional[bool] = None
    inbound_workflow_id: Optional[int] = None
    inbound_workflow_name: Optional[str] = None
    shared_with: List[SharedWithWorkspace] = Field(default_factory=list)


class SharedAgentShare(BaseModel):
    workflow_id: int
    name: str
    share_percent: float


class NumberSharedWithMe(BaseModel):
    """A number another workspace owns and routes (partly) to this
    workspace's agents. Read-only here: only the owner changes it."""

    address: str
    is_active: bool
    owner_organization_id: int
    owner_label: str
    routing_group_id: int
    routing_group_name: str
    routing_group_active: bool
    sticky_by_caller: bool
    my_share_percent: float
    other_share_percent: float
    my_agents: List[SharedAgentShare]


class SharedNumbersResponse(BaseModel):
    organization_label: str
    # Numbers this workspace owns; it controls their routing.
    owned: List[OwnedNumber]
    # Numbers other workspaces share with this one (read-only).
    shared_with_me: List[NumberSharedWithMe]
