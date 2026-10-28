"""Database access for inbound routing groups.

A routing group splits inbound calls on a set of phone numbers across several
workflows by weight. Numbers join a group through
``telephony_phone_numbers.inbound_routing_group_id``; the resolver that reads
these rows lives in ``api/services/telephony/inbound_routing.py``.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import func, or_, update
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from api.db.base_client import BaseDBClient
from api.db.models import (
    InboundRoutingAuditModel,
    InboundRoutingGroupModel,
    InboundRoutingTargetModel,
    TelephonyConfigurationModel,
    TelephonyPhoneNumberModel,
    WorkflowModel,
    WorkflowRunModel,
)


class InboundRoutingClient(BaseDBClient):
    async def get_inbound_routing_group_with_targets(
        self, group_id: int
    ) -> Optional[InboundRoutingGroupModel]:
        """Unscoped load used by the inbound-call resolver, which derives the
        org from the phone row and checks the group belongs to the same org."""
        async with self.async_session() as session:
            result = await session.execute(
                select(InboundRoutingGroupModel)
                .options(selectinload(InboundRoutingGroupModel.targets))
                .where(InboundRoutingGroupModel.id == group_id)
            )
            return result.scalars().first()

    async def list_inbound_routing_groups(
        self, organization_id: int
    ) -> List[InboundRoutingGroupModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(InboundRoutingGroupModel)
                .options(selectinload(InboundRoutingGroupModel.targets))
                .where(InboundRoutingGroupModel.organization_id == organization_id)
                .order_by(InboundRoutingGroupModel.created_at)
            )
            return list(result.scalars().all())

    async def get_inbound_routing_group(
        self, group_id: int, organization_id: int
    ) -> Optional[InboundRoutingGroupModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(InboundRoutingGroupModel)
                .options(selectinload(InboundRoutingGroupModel.targets))
                .where(
                    InboundRoutingGroupModel.id == group_id,
                    InboundRoutingGroupModel.organization_id == organization_id,
                )
            )
            return result.scalars().first()

    async def create_inbound_routing_group(
        self,
        organization_id: int,
        name: str,
        is_active: bool,
        sticky_by_caller: bool,
        targets: Sequence[Dict[str, int]],
    ) -> InboundRoutingGroupModel:
        async with self.async_session() as session:
            group = InboundRoutingGroupModel(
                organization_id=organization_id,
                name=name,
                is_active=is_active,
                sticky_by_caller=sticky_by_caller,
                targets=[
                    InboundRoutingTargetModel(
                        workflow_id=t["workflow_id"], weight=t["weight"]
                    )
                    for t in targets
                ],
            )
            session.add(group)
            await session.flush()
            # Read before commit: the commit expires the instance.
            group_id = group.id
            await session.commit()
        return await self.get_inbound_routing_group(group_id, organization_id)

    async def update_inbound_routing_group(
        self,
        group_id: int,
        organization_id: int,
        name: Optional[str] = None,
        is_active: Optional[bool] = None,
        sticky_by_caller: Optional[bool] = None,
        targets: Optional[Sequence[Dict[str, int]]] = None,
    ) -> Optional[InboundRoutingGroupModel]:
        """Partial update. ``targets``, when given, replaces the whole set."""
        async with self.async_session() as session:
            result = await session.execute(
                select(InboundRoutingGroupModel)
                .options(selectinload(InboundRoutingGroupModel.targets))
                .where(
                    InboundRoutingGroupModel.id == group_id,
                    InboundRoutingGroupModel.organization_id == organization_id,
                )
            )
            group = result.scalars().first()
            if not group:
                return None
            if name is not None:
                group.name = name
            if is_active is not None:
                group.is_active = is_active
            if sticky_by_caller is not None:
                group.sticky_by_caller = sticky_by_caller
            if targets is not None:
                # Update in place by workflow so the unique (group, workflow)
                # constraint never sees a delete and an insert of the same pair.
                existing = {t.workflow_id: t for t in group.targets}
                wanted = {t["workflow_id"]: t["weight"] for t in targets}
                for workflow_id, target in existing.items():
                    if workflow_id not in wanted:
                        group.targets.remove(target)
                    elif target.weight != wanted[workflow_id]:
                        target.weight = wanted[workflow_id]
                for workflow_id, weight in wanted.items():
                    if workflow_id not in existing:
                        group.targets.append(
                            InboundRoutingTargetModel(
                                workflow_id=workflow_id, weight=weight
                            )
                        )
            group.updated_at = func.now()
            await session.commit()
        return await self.get_inbound_routing_group(group_id, organization_id)

    async def delete_inbound_routing_group(
        self, group_id: int, organization_id: int
    ) -> bool:
        """Delete a group. Its numbers fall back to their inbound_workflow_id
        (the FK is ON DELETE SET NULL)."""
        async with self.async_session() as session:
            result = await session.execute(
                select(InboundRoutingGroupModel).where(
                    InboundRoutingGroupModel.id == group_id,
                    InboundRoutingGroupModel.organization_id == organization_id,
                )
            )
            group = result.scalars().first()
            if not group:
                return False
            await session.execute(
                update(TelephonyPhoneNumberModel)
                .where(TelephonyPhoneNumberModel.inbound_routing_group_id == group_id)
                .values(inbound_routing_group_id=None)
            )
            await session.delete(group)
            await session.commit()
            return True

    async def count_numbers_by_inbound_routing_group(
        self, organization_id: int
    ) -> Dict[int, int]:
        async with self.async_session() as session:
            result = await session.execute(
                select(
                    TelephonyPhoneNumberModel.inbound_routing_group_id,
                    func.count(TelephonyPhoneNumberModel.id),
                )
                .where(
                    TelephonyPhoneNumberModel.organization_id == organization_id,
                    TelephonyPhoneNumberModel.inbound_routing_group_id.is_not(None),
                )
                .group_by(TelephonyPhoneNumberModel.inbound_routing_group_id)
            )
            return {group_id: count for group_id, count in result.all()}

    async def summarize_numbers_for_inbound_routing_group(
        self, group_id: int, organization_id: int
    ) -> List[Dict[str, Any]]:
        """One row per telephony configuration in the org: how many numbers
        it has, how many are in this group, and how many in another group."""
        in_this = func.count(TelephonyPhoneNumberModel.id).filter(
            TelephonyPhoneNumberModel.inbound_routing_group_id == group_id
        )
        in_other = func.count(TelephonyPhoneNumberModel.id).filter(
            TelephonyPhoneNumberModel.inbound_routing_group_id.is_not(None),
            TelephonyPhoneNumberModel.inbound_routing_group_id != group_id,
        )
        async with self.async_session() as session:
            result = await session.execute(
                select(
                    TelephonyConfigurationModel.id,
                    TelephonyConfigurationModel.name,
                    TelephonyConfigurationModel.provider,
                    func.count(TelephonyPhoneNumberModel.id),
                    in_this,
                    in_other,
                )
                .join(
                    TelephonyPhoneNumberModel,
                    TelephonyPhoneNumberModel.telephony_configuration_id
                    == TelephonyConfigurationModel.id,
                    isouter=True,
                )
                .where(TelephonyConfigurationModel.organization_id == organization_id)
                .group_by(
                    TelephonyConfigurationModel.id,
                    TelephonyConfigurationModel.name,
                    TelephonyConfigurationModel.provider,
                )
                .order_by(TelephonyConfigurationModel.id)
            )
            return [
                {
                    "telephony_configuration_id": config_id,
                    "name": name,
                    "provider": provider,
                    "total_numbers": total,
                    "in_this_group": this,
                    "in_other_group": other,
                }
                for config_id, name, provider, total, this, other in result.all()
            ]

    @staticmethod
    def _number_selector(
        organization_id: int,
        telephony_configuration_ids: Optional[Sequence[int]],
        phone_number_ids: Optional[Sequence[int]],
        inbound_workflow_id: Optional[int],
    ):
        """WHERE clause for a bulk attach/detach: the org's numbers matching
        ANY of the given selectors. Always org-scoped."""
        selectors = []
        if telephony_configuration_ids:
            selectors.append(
                TelephonyPhoneNumberModel.telephony_configuration_id.in_(
                    list(telephony_configuration_ids)
                )
            )
        if phone_number_ids:
            selectors.append(TelephonyPhoneNumberModel.id.in_(list(phone_number_ids)))
        if inbound_workflow_id is not None:
            selectors.append(
                TelephonyPhoneNumberModel.inbound_workflow_id == inbound_workflow_id
            )
        if not selectors:
            return None
        return (TelephonyPhoneNumberModel.organization_id == organization_id) & or_(
            *selectors
        )

    async def attach_numbers_to_inbound_routing_group(
        self,
        group_id: int,
        organization_id: int,
        telephony_configuration_ids: Optional[Sequence[int]] = None,
        phone_number_ids: Optional[Sequence[int]] = None,
        inbound_workflow_id: Optional[int] = None,
        dry_run: bool = False,
    ) -> int:
        """Move the selected numbers into the group (out of any other group).
        Returns how many numbers change group; with ``dry_run`` nothing is
        written."""
        where = self._number_selector(
            organization_id,
            telephony_configuration_ids,
            phone_number_ids,
            inbound_workflow_id,
        )
        if where is None:
            return 0
        where = where & or_(
            TelephonyPhoneNumberModel.inbound_routing_group_id.is_(None),
            TelephonyPhoneNumberModel.inbound_routing_group_id != group_id,
        )
        async with self.async_session() as session:
            if dry_run:
                result = await session.execute(
                    select(func.count(TelephonyPhoneNumberModel.id)).where(where)
                )
                return result.scalar_one()
            result = await session.execute(
                update(TelephonyPhoneNumberModel)
                .where(where)
                .values(inbound_routing_group_id=group_id)
                .execution_options(synchronize_session="fetch")
            )
            await session.commit()
            return result.rowcount

    async def detach_numbers_from_inbound_routing_group(
        self,
        group_id: int,
        organization_id: int,
        telephony_configuration_ids: Optional[Sequence[int]] = None,
        phone_number_ids: Optional[Sequence[int]] = None,
        inbound_workflow_id: Optional[int] = None,
        detach_all: bool = False,
        dry_run: bool = False,
    ) -> int:
        """Take the selected numbers out of the group; they fall back to their
        inbound_workflow_id on the next call."""
        in_group = (TelephonyPhoneNumberModel.organization_id == organization_id) & (
            TelephonyPhoneNumberModel.inbound_routing_group_id == group_id
        )
        if detach_all:
            where = in_group
        else:
            selector = self._number_selector(
                organization_id,
                telephony_configuration_ids,
                phone_number_ids,
                inbound_workflow_id,
            )
            if selector is None:
                return 0
            where = in_group & selector
        async with self.async_session() as session:
            if dry_run:
                result = await session.execute(
                    select(func.count(TelephonyPhoneNumberModel.id)).where(where)
                )
                return result.scalar_one()
            result = await session.execute(
                update(TelephonyPhoneNumberModel)
                .where(where)
                .values(inbound_routing_group_id=None)
                .execution_options(synchronize_session="fetch")
            )
            await session.commit()
            return result.rowcount

    async def count_inbound_runs_by_workflow_for_routing_group(
        self, group_id: int, organization_id: int, since: datetime
    ) -> Dict[int, int]:
        """Inbound runs created since ``since`` that the resolver routed
        through this group, counted by the workflow that took the call."""
        group_id_in_meta = WorkflowRunModel.initial_context.op("->")(
            "inbound_routing"
        ).op("->>")("group_id")
        async with self.async_session() as session:
            result = await session.execute(
                select(WorkflowRunModel.workflow_id, func.count(WorkflowRunModel.id))
                .join(WorkflowModel, WorkflowModel.id == WorkflowRunModel.workflow_id)
                .where(
                    WorkflowRunModel.created_at >= since,
                    WorkflowModel.organization_id == organization_id,
                    group_id_in_meta == str(group_id),
                )
                .group_by(WorkflowRunModel.workflow_id)
            )
            return {workflow_id: count for workflow_id, count in result.all()}

    async def add_inbound_routing_audit(
        self,
        organization_id: int,
        group_id: Optional[int],
        user_id: Optional[int],
        action: str,
        details: Dict[str, Any],
    ) -> InboundRoutingAuditModel:
        async with self.async_session() as session:
            row = InboundRoutingAuditModel(
                organization_id=organization_id,
                group_id=group_id,
                user_id=user_id,
                action=action,
                details=details,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row

    async def list_inbound_routing_audit(
        self,
        organization_id: int,
        group_id: Optional[int] = None,
        limit: int = 100,
    ) -> List[InboundRoutingAuditModel]:
        async with self.async_session() as session:
            query = (
                select(InboundRoutingAuditModel)
                .options(selectinload(InboundRoutingAuditModel.user))
                .where(InboundRoutingAuditModel.organization_id == organization_id)
                .order_by(InboundRoutingAuditModel.created_at.desc())
                .limit(limit)
            )
            if group_id is not None:
                query = query.where(InboundRoutingAuditModel.group_id == group_id)
            result = await session.execute(query)
            return list(result.scalars().all())
