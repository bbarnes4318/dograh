import asyncio
import os
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Optional

from loguru import logger

from api.db import db_client
from api.db.models import QueuedRunModel, WorkflowRunModel
from api.enums import WorkflowRunMode, WorkflowRunState
from api.services.call_concurrency import (
    CallConcurrencyLimitError,
    CallConcurrencySlot,
    call_concurrency,
)
from api.services.campaign.areacode_state import state_for_number
from api.services.campaign.circuit_breaker import circuit_breaker
from api.services.campaign.dialing_windows import (
    DialingPolicy,
    dialing_config,
    rank_from_numbers_by_locality,
    resolve_lead_timezone,
    seconds_until_local_window,
)
from api.services.campaign.errors import (
    ConcurrentSlotAcquisitionError,
    PhoneNumberPoolExhaustedError,
)
from api.services.campaign.rate_limiter import rate_limiter
from api.services.quota_service import authorize_workflow_run_start
from api.services.workflow.run_creation import prepare_workflow_run_inputs
from api.utils.common import get_backend_endpoints
from api.utils.telephony_address import is_dialable_pstn

NO_CALLER_ID_FOR_STATE_REASON = "NO_CALLER_ID_AVAILABLE_FOR_DESTINATION_STATE"


class NoCallerIdForDestinationStateError(Exception):
    """Strict state-matched caller ID found no same-state number for the lead.

    The queued run fails rather than dialling with an out-of-state caller ID.
    """

    def __init__(
        self,
        *,
        destination: str,
        destination_state: str | None,
        destination_reason: str,
        campaign_id: int,
        organization_id: int,
    ) -> None:
        self.destination = destination
        self.destination_state = destination_state
        self.destination_reason = destination_reason
        self.campaign_id = campaign_id
        self.organization_id = organization_id
        super().__init__(
            f"{NO_CALLER_ID_FOR_STATE_REASON}: destination={destination} "
            f"destination_state={destination_state} state_reason={destination_reason} "
            f"campaign_id={campaign_id} organization_id={organization_id}"
        )


def resolve_state_cid_policy(campaign) -> str:
    """Per-campaign state-matched caller ID policy.

    Order: ``campaign.orchestrator_metadata.state_cid_policy``, then env
    ``DOGRAH_STATE_CID_POLICY``, else ``"off"``.

    - ``off``: whole-pool selection (local-presence ranking still applies).
    - ``prefer``: same-state caller ID when one is free, else any.
    - ``strict``: same-state only; with none in the pool the call fails with
      ``NO_CALLER_ID_AVAILABLE_FOR_DESTINATION_STATE``.
    """
    policy = None
    metadata = campaign.orchestrator_metadata
    if isinstance(metadata, dict):
        policy = metadata.get("state_cid_policy")
    if not policy:
        policy = os.environ.get("DOGRAH_STATE_CID_POLICY", "off")
    policy = str(policy).strip().lower()
    return policy if policy in ("off", "prefer", "strict") else "off"


def resolve_transfer_destination(queued_run, campaign) -> Optional[str]:
    """Transfer target for this call: the lead's, then the campaign's, then
    the deployment default (``CAMPAIGN_DEFAULT_TRANSFER_DESTINATION``)."""
    metadata = campaign.orchestrator_metadata
    return (
        queued_run.context_variables.get("transfer_destination")
        or (
            metadata.get("transfer_destination") if isinstance(metadata, dict) else None
        )
        or os.environ.get("CAMPAIGN_DEFAULT_TRANSFER_DESTINATION")
        or None
    )


if TYPE_CHECKING:
    # Type-only — importing api.services.telephony eagerly triggers the
    # provider package init, which can pull in this module via the routes
    # chain and create a circular import. Runtime calls below lazy-import the
    # factory helpers inside methods instead.
    from api.services.telephony.base import TelephonyProvider


class CampaignCallDispatcher:
    """Manages rate-limited and concurrent-limited call dispatching"""

    async def get_provider_for_campaign(self, campaign) -> "TelephonyProvider":
        """Get the telephony provider pinned to this campaign's config. Falls back
        to the org's default config for legacy campaigns whose
        ``telephony_configuration_id`` was never backfilled."""
        from api.services.telephony.factory import (
            get_default_telephony_provider,
            get_telephony_provider_by_id,
        )

        if campaign.telephony_configuration_id:
            return await get_telephony_provider_by_id(
                campaign.telephony_configuration_id, campaign.organization_id
            )
        logger.warning(
            f"Campaign {campaign.id} has no telephony_configuration_id; "
            f"falling back to org default for {campaign.organization_id}"
        )
        return await get_default_telephony_provider(campaign.organization_id)

    async def get_org_concurrent_limit(self, organization_id: int) -> int:
        """Get the concurrent call limit for an organization."""
        return await call_concurrency.get_org_concurrent_limit(organization_id)

    async def process_batch(self, campaign_id: int, batch_size: int = 10) -> int:
        """
        Processes a batch of queued runs with priority for scheduled retries.
        Thread-safe: uses SELECT FOR UPDATE SKIP LOCKED to prevent concurrent processing.
        Returns: number of processed runs
        """
        # Get campaign details
        campaign = await db_client.get_campaign_by_id(campaign_id)
        if not campaign:
            raise ValueError(f"Campaign {campaign_id} not found")

        # Check if campaign is in running state
        if campaign.state != "running":
            logger.info(
                f"Campaign {campaign_id} is not in running state: {campaign.state}"
            )
            return 0

        # Atomically claim queued runs for processing (thread-safe)
        # This uses SELECT FOR UPDATE SKIP LOCKED to prevent race conditions
        queued_runs = await db_client.claim_queued_runs_for_processing(
            campaign_id=campaign_id,
            scheduled_before=datetime.now(UTC),
            limit=batch_size,
        )

        if not queued_runs:
            logger.info(f"No more queued runs for campaign {campaign_id}")
            return 0

        # Initialize from_number pool for this campaign's telephony config.
        try:
            provider = await self.get_provider_for_campaign(campaign)
            if provider.from_numbers:
                await rate_limiter.initialize_from_number_pool(
                    campaign.organization_id,
                    provider.from_numbers,
                    telephony_configuration_id=campaign.telephony_configuration_id,
                )
        except Exception as e:
            logger.warning(f"Failed to initialize from_number pool: {e}")

        processed_count = 0
        processed_run_ids: set[int] = set()
        policy = dialing_config(campaign)
        for i, queued_run in enumerate(queued_runs):
            try:
                # Calling windows are defined in the *called party's* local
                # time. A lead outside their own window is put back on the
                # queue for when it opens, rather than dialled now.
                if await self._defer_outside_local_window(queued_run, policy):
                    processed_run_ids.add(queued_run.id)
                    continue

                # Apply rate limiting, i.e lets not initiate more than rate_limit_per_second
                # calls per second. It is different than concurrency limit.
                await self.apply_rate_limit(
                    campaign.organization_id, campaign.rate_limit_per_second
                )

                # Acquire concurrent slot - waits until a slot is available
                concurrency_slot = await self.acquire_concurrent_slot(
                    campaign.organization_id, campaign
                )

                # Dispatch the call
                workflow_run = await self.dispatch_call(
                    queued_run, campaign, concurrency_slot
                )

                # Update queued run as processed
                await db_client.update_queued_run(
                    queued_run_id=queued_run.id,
                    state="processed",
                    workflow_run_id=workflow_run.id,
                    processed_at=datetime.now(UTC),
                )

                processed_count += 1
                processed_run_ids.add(queued_run.id)

                # Update campaign processed count
                await db_client.update_campaign(
                    campaign_id=campaign_id, processed_rows=campaign.processed_rows + 1
                )

            except asyncio.CancelledError:
                logger.warning(
                    f"Campaign {campaign_id} batch cancelled; returning claimed "
                    "queued runs that were not dispatched"
                )
                await self._return_unprocessed_claims(
                    queued_runs, processed_run_ids, reason="task_cancelled"
                )
                raise

            except PhoneNumberPoolExhaustedError as e:
                logger.warning(
                    f"Phone number pool exhausted for campaign {campaign_id}; "
                    "returning claimed queued runs that were not dispatched: "
                    f"{e}"
                )
                await self._return_unprocessed_claims(
                    queued_runs,
                    processed_run_ids,
                    reason="phone_number_pool_exhausted",
                )
                # Re-raise to propagate to process_campaign_batch
                raise

            except ConcurrentSlotAcquisitionError as e:
                logger.warning(
                    f"Concurrent slot acquisition failed for campaign {campaign_id}; "
                    "returning claimed queued runs that were not dispatched: "
                    f"{e}"
                )
                await self._return_unprocessed_claims(
                    queued_runs,
                    processed_run_ids,
                    reason="concurrent_slot_acquisition_failed",
                )
                # Re-raise to propagate to process_campaign_batch
                raise

            except Exception as e:
                logger.warning(f"Error processing queued run {queued_run.id}: {e}")

                # Mark the queued run as failed to prevent infinite retry loops
                try:
                    await db_client.update_queued_run(
                        queued_run_id=queued_run.id,
                        state="failed",
                        processed_at=datetime.now(UTC),
                    )
                    logger.info(
                        f"Marked queued run {queued_run.id} as failed due to error: {e}"
                    )
                except Exception as update_error:
                    logger.error(
                        f"Failed to mark queued run {queued_run.id} as failed: {update_error}"
                    )

        return processed_count

    async def _defer_outside_local_window(
        self,
        queued_run: QueuedRunModel,
        policy: "DialingPolicy",
    ) -> bool:
        """Put a lead back on the queue when it's outside their local window.

        Returns True when the run was deferred and must not be dialled.
        Failing to reschedule is not a reason to dial outside the window:
        the run goes back to ``queued`` either way and the next batch retries.
        """
        if not policy.schedule_enabled or not policy.per_lead_timezone:
            return False

        phone_number = (queued_run.context_variables or {}).get("phone_number")
        timezone = resolve_lead_timezone(
            queued_run.context_variables, phone_number, policy.timezone
        )
        wait_seconds = seconds_until_local_window(policy.slots, timezone)
        if wait_seconds is None:
            return False

        scheduled_for = datetime.now(UTC) + timedelta(seconds=wait_seconds)
        logger.info(
            f"Queued run {queued_run.id} is outside its local window "
            f"({timezone}); rescheduling for {scheduled_for.isoformat()}"
        )
        try:
            await db_client.update_queued_run(
                queued_run_id=queued_run.id,
                state="queued",
                scheduled_for=scheduled_for,
            )
        except Exception as e:
            logger.error(
                f"Could not reschedule queued run {queued_run.id} outside its "
                f"local window: {e}"
            )
            await self._return_unprocessed_claims(
                [queued_run], set(), reason="local_window_reschedule_failed"
            )
        return True

    async def _return_unprocessed_claims(
        self,
        queued_runs: list[QueuedRunModel],
        processed_run_ids: set[int],
        *,
        reason: str,
    ) -> None:
        queued_run_ids = [
            queued_run.id
            for queued_run in queued_runs
            if queued_run.id not in processed_run_ids
        ]
        if not queued_run_ids:
            return

        try:
            returned_count = (
                await db_client.return_processing_queued_runs_without_workflow(
                    queued_run_ids
                )
            )
            logger.info(
                f"Returned {returned_count}/{len(queued_run_ids)} claimed queued runs "
                f"back to queued state; reason={reason}; "
                f"queued_run_ids={queued_run_ids}"
            )
        except Exception as revert_error:
            logger.error(
                f"Failed to return claimed queued runs; reason={reason}; "
                f"queued_run_ids={queued_run_ids}; error={revert_error}"
            )

    async def dispatch_call(
        self,
        queued_run: QueuedRunModel,
        campaign: any,
        concurrency_slot: CallConcurrencySlot,
    ) -> Optional[WorkflowRunModel]:
        """Creates workflow run and initiates call. Requires a pre-acquired slot."""
        from_number = None
        workflow_run = None
        slot_bound = False

        try:
            # Get workflow details
            workflow = await db_client.get_workflow(
                campaign.workflow_id,
                organization_id=campaign.organization_id,
            )
            if not workflow:
                raise ValueError(f"Workflow {campaign.workflow_id} not found")

            # Extract phone number
            phone_number = queued_run.context_variables.get("phone_number")
            if not phone_number:
                raise ValueError(f"No phone number in queued run {queued_run.id}")
            if not is_dialable_pstn(phone_number):
                raise ValueError(
                    f"Undialable phone number in queued run {queued_run.id}"
                )

            # Get provider for this campaign's pinned telephony config.
            provider = await self.get_provider_for_campaign(campaign)
            workflow_run_mode = provider.PROVIDER_NAME

            # Acquire a unique from_number from the pool scoped to this campaign's
            # telephony configuration so orgs with multiple configs don't leak
            # caller IDs across configs. Prefer a caller ID that looks local to
            # the lead — people answer a local number far more often — and honour
            # any per-number daily cap so one ID doesn't get spam-labelled.
            dialing = dialing_config(campaign)
            preferred_numbers = (
                rank_from_numbers_by_locality(provider.from_numbers or [], phone_number)
                if dialing.local_presence
                else None
            )

            # State-matched caller ID: per policy, restrict selection to pool
            # numbers in the lead's state. Selection stays server-side and
            # atomic in the rate limiter; local-presence ranking still applies
            # inside the allowed subset.
            state_cid_policy = resolve_state_cid_policy(campaign)
            dest_state, dest_state_reason = state_for_number(phone_number)
            allowed_from_numbers: Optional[list[str]] = None
            state_match_active = False
            if state_cid_policy != "off":
                pool_numbers = list(provider.from_numbers or [])
                same_state = (
                    [n for n in pool_numbers if state_for_number(n)[0] == dest_state]
                    if dest_state
                    else []
                )
                if same_state:
                    allowed_from_numbers = same_state
                    state_match_active = True
                elif state_cid_policy == "strict":
                    logger.warning(
                        "STATE-CID strict: no same-state caller ID — failing call. "
                        f"destination_state={dest_state} "
                        f"state_reason={dest_state_reason} "
                        f"campaign_id={campaign.id} pool_size={len(pool_numbers)}"
                    )
                    raise NoCallerIdForDestinationStateError(
                        destination=phone_number,
                        destination_state=dest_state,
                        destination_reason=dest_state_reason,
                        campaign_id=campaign.id,
                        organization_id=campaign.organization_id,
                    )
                else:
                    logger.info(
                        "STATE-CID prefer: no same-state caller ID for "
                        f"destination_state={dest_state} "
                        f"(reason={dest_state_reason}); using whole pool. "
                        f"campaign_id={campaign.id}"
                    )

            acquire_kwargs = dict(
                telephony_configuration_id=campaign.telephony_configuration_id,
                preferred_numbers=preferred_numbers,
                daily_cap=dialing.from_number_daily_cap,
                # Reset the cap on the campaign's calling day, not the UTC day.
                cap_timezone=dialing.timezone,
            )
            from_number = await self.acquire_from_number(
                campaign.organization_id,
                allowed_numbers=allowed_from_numbers,
                # prefer: don't stall the batch on a busy same-state subset.
                timeout=(
                    5 if (state_match_active and state_cid_policy == "prefer") else 600
                ),
                **acquire_kwargs,
            )
            if (
                from_number is None
                and state_match_active
                and state_cid_policy == "prefer"
            ):
                logger.info(
                    f"STATE-CID prefer: same-state pool busy for {dest_state}; "
                    f"using whole pool. campaign_id={campaign.id}"
                )
                state_match_active = False
                from_number = await self.acquire_from_number(
                    campaign.organization_id, **acquire_kwargs
                )
            if from_number is None:
                if state_match_active and state_cid_policy == "strict":
                    # Same-state numbers exist but are all in use: transient, so
                    # the run goes back to the queue like any exhausted pool.
                    logger.warning(
                        f"STATE-CID strict: same-state pool ({dest_state}) busy; "
                        f"deferring call. campaign_id={campaign.id}"
                    )
                raise PhoneNumberPoolExhaustedError(
                    organization_id=campaign.organization_id
                )

            caller_id_state, _ = state_for_number(from_number)

            logger.info(f"Provider name: {provider.PROVIDER_NAME}")
            logger.info(f"Queued run context: {queued_run.context_variables}")

            # Merge context variables (queued_run context already includes retry info if applicable)
            initial_context = {
                **queued_run.context_variables,
                "campaign_id": campaign.id,
                "provider": provider.PROVIDER_NAME,
                "source_uuid": queued_run.source_uuid,
                "caller_number": from_number,
                "called_number": phone_number,
                # State-matched caller ID decision, kept for reporting.
                "destination_state": dest_state,
                "destination_state_reason": dest_state_reason,
                "caller_id_state": caller_id_state,
                "state_cid_policy": state_cid_policy,
                "caller_id_state_match": bool(
                    dest_state and caller_id_state == dest_state
                ),
                "telephony_configuration_id": campaign.telephony_configuration_id,
            }
            transfer_destination = resolve_transfer_destination(queued_run, campaign)
            if transfer_destination:
                initial_context["transfer_destination"] = transfer_destination

            logger.info(f"Final initial_context: {initial_context}")

            # Create workflow run with queued_run_id tracking
            workflow_run_name = f"WR-CAMPAIGN-{campaign.id}-{queued_run.id}"
            run_inputs = await prepare_workflow_run_inputs(db_client, workflow)
            workflow_run = await db_client.create_workflow_run(
                name=workflow_run_name,
                workflow_id=campaign.workflow_id,
                mode=workflow_run_mode,
                user_id=campaign.created_by,
                initial_context=initial_context,
                campaign_id=campaign.id,
                queued_run_id=queued_run.id,  # Link to queued run for retry tracking
                organization_id=campaign.organization_id,
                definition_id=run_inputs.definition_id,
            )
            await call_concurrency.bind_workflow_run(concurrency_slot, workflow_run.id)
            slot_bound = True

            # Store from_number mapping for cleanup on call completion
            await rate_limiter.store_workflow_from_number_mapping(
                workflow_run.id,
                campaign.organization_id,
                from_number,
                telephony_configuration_id=campaign.telephony_configuration_id,
            )
        except Exception:
            # Release slot and from_number on error
            if slot_bound and workflow_run:
                await call_concurrency.release_workflow_run_slot(workflow_run.id)
            else:
                await call_concurrency.release_slot(concurrency_slot)
            if from_number:
                await rate_limiter.release_from_number(
                    campaign.organization_id,
                    from_number,
                    telephony_configuration_id=campaign.telephony_configuration_id,
                )
            raise

        # Add "retry" tag if this is a retry call
        if queued_run.context_variables.get("is_retry"):
            retry_reason = queued_run.context_variables.get("retry_reason", "unknown")
            await db_client.update_workflow_run(
                run_id=workflow_run.id,
                gathered_context={
                    "call_tags": ["retry", f"retry_reason_{retry_reason}"]
                },
            )

        quota_result = await authorize_workflow_run_start(
            workflow_id=campaign.workflow_id,
            organization_id=campaign.organization_id,
            workflow_run_id=workflow_run.id,
        )
        if not quota_result.has_quota:
            error_message = quota_result.error_message or "Quota exceeded"
            logger.warning(
                f"Campaign {campaign.id} quota check failed for workflow run "
                f"{workflow_run.id}: {error_message}"
            )
            await db_client.update_workflow_run(
                run_id=workflow_run.id,
                is_completed=True,
                state=WorkflowRunState.COMPLETED.value,
                gathered_context={"error": error_message},
            )

            await self.release_call_slot(workflow_run.id)

            raise ValueError(error_message)

        # Initiate call via telephony provider
        try:
            # Construct webhook URL with parameters
            backend_endpoint, _ = await get_backend_endpoints()
            webhook_endpoint = provider.WEBHOOK_ENDPOINT
            webhook_url = (
                f"{backend_endpoint}/api/v1/telephony/{webhook_endpoint}"
                f"?workflow_id={campaign.workflow_id}"
                f"&workflow_run_id={workflow_run.id}"
                f"&organization_id={campaign.organization_id}"
            )

            call_result = await provider.initiate_call(
                to_number=phone_number,
                webhook_url=webhook_url,
                workflow_run_id=workflow_run.id,
                from_number=from_number,
                workflow_id=campaign.workflow_id,
                organization_id=campaign.organization_id,
            )

            # Store provider type and metadata in gathered_context
            # (required for WebSocket handler to route to correct provider)
            await db_client.update_workflow_run(
                run_id=workflow_run.id,
                gathered_context={
                    "provider": provider.PROVIDER_NAME,
                    **(call_result.provider_metadata or {}),
                },
            )

            logger.info(
                f"Call initiated for workflow run {workflow_run.id}, Call ID: {call_result.call_id}"
            )

            # Map channel -> run at dial time so the ARI manager can release the
            # concurrency slot and caller ID for a call that never answers (no
            # StasisStart, so the normal StasisEnd release never runs). The key
            # matches ari_manager's _CHANNEL_KEY_PREFIX.
            if (
                provider.PROVIDER_NAME == WorkflowRunMode.ARI.value
                and call_result.call_id
            ):
                try:
                    redis_client = await rate_limiter._get_redis()
                    await redis_client.set(
                        f"ari:channel:{call_result.call_id}",
                        str(workflow_run.id),
                        ex=3600,
                    )
                except Exception as map_err:
                    logger.warning(f"Failed to record channel->run mapping: {map_err}")

        except Exception as e:
            logger.error(
                f"Failed to initiate call for workflow run {workflow_run.id}: {e}"
            )

            # Update workflow run as failed
            telephony_callback_logs = workflow_run.logs.get(
                "telephony_status_callbacks", []
            )
            telephony_callback_log = {
                "status": "failed",
                "timestamp": datetime.now(UTC).isoformat(),
                "data": {"error": str(e)},
            }
            telephony_callback_logs.append(telephony_callback_log)
            await db_client.update_workflow_run(
                run_id=workflow_run.id,
                is_completed=True,
                state=WorkflowRunState.COMPLETED.value,
                gathered_context={
                    "error": str(e),
                },
                logs={
                    "telephony_status_callbacks": telephony_callback_logs,
                },
            )

            # Record call initiation failure in circuit breaker
            await circuit_breaker.record_and_evaluate(
                campaign.id,
                is_failure=True,
                workflow_run_id=workflow_run.id,
                reason="call_initiation_failed",
            )

            await self.release_call_slot(workflow_run.id)

            raise

        return workflow_run

    async def apply_rate_limit(self, organization_id: int, rate_limit: int) -> None:
        """
        Enforces rate limiting - waits if necessary to comply with rate limit

        Example usage:
        ```
        # This will wait up to 1 second if needed to respect rate limit
        await self.apply_rate_limit(org_id, 1)  # 1 call per second
        await twilio.initiate_call(...)  # Now safe to call
        ```
        """
        max_wait = 1.0  # Maximum time to wait for a slot
        start_time = time.time()

        while True:
            # Try to acquire token
            if await rate_limiter.acquire_token(organization_id, rate_limit):
                return  # Got permission to proceed

            # Check how long to wait
            wait_time = await rate_limiter.get_next_available_slot(
                organization_id, rate_limit
            )

            # Don't wait forever
            if time.time() - start_time + wait_time > max_wait:
                raise TimeoutError("Rate limit timeout - try again later")

            # Wait for next available slot
            await asyncio.sleep(wait_time)

    async def acquire_concurrent_slot(
        self, organization_id: int, campaign: any, timeout: float = 600
    ) -> CallConcurrencySlot:
        """
        Acquires a concurrent call slot - waits if necessary until a slot is available.

        Args:
            organization_id: The organization ID
            campaign: The campaign object
            timeout: Maximum time to wait for a slot (default 10 minutes)

        Returns the slot which must be released when the call completes.

        Raises:
            ConcurrentSlotAcquisitionError: If slot cannot be acquired within timeout
        """
        # Check for campaign-level max_concurrency in orchestrator_metadata.
        # It caps this campaign's own concurrent calls via a campaign-scoped
        # counter — the org-wide limit still applies on top, but calls from
        # other sources (WebRTC, inbound, other campaigns) don't count
        # against the campaign's cap.
        campaign_max_concurrency = None
        if campaign.orchestrator_metadata:
            campaign_max_concurrency = campaign.orchestrator_metadata.get(
                "max_concurrency"
            )

        try:
            return await call_concurrency.acquire_org_slot(
                organization_id,
                source=f"campaign:{campaign.id}",
                timeout=timeout,
                scope_key=(
                    f"campaign:{campaign.id}"
                    if campaign_max_concurrency is not None
                    else None
                ),
                scope_max_concurrent=campaign_max_concurrency,
                retry_interval=1,
            )
        except CallConcurrencyLimitError as e:
            raise ConcurrentSlotAcquisitionError(
                organization_id=organization_id,
                campaign_id=campaign.id,
                wait_time=e.wait_time,
            ) from e

    async def acquire_from_number(
        self,
        organization_id: int,
        telephony_configuration_id: int | None,
        timeout: float = 600,
        preferred_numbers: Optional[list[str]] = None,
        daily_cap: Optional[int] = None,
        cap_timezone: Optional[str] = None,
        allowed_numbers: Optional[list[str]] = None,
    ) -> Optional[str]:
        """
        Acquire a from_number from the (org, telephony config) pool with retry.
        Waits up to timeout seconds, polling every 1s.

        Args:
            preferred_numbers: Ranked caller IDs to try first (local presence).
            daily_cap: Max dials per caller ID per day, or None for no cap.
            cap_timezone: Zone whose calendar day the cap resets on.
            allowed_numbers: Restrict acquisition to this subset (state-matched
                caller ID). An empty list returns None immediately.

        Returns:
            The acquired phone number as a string, or None if timeout is exceeded.
        """
        if allowed_numbers is not None and len(allowed_numbers) == 0:
            return None

        wait_start = time.time()

        while True:
            from_number = await rate_limiter.acquire_from_number(
                organization_id,
                telephony_configuration_id,
                preferred_numbers=preferred_numbers,
                daily_cap=daily_cap,
                cap_timezone=cap_timezone,
                allowed_numbers=allowed_numbers,
            )
            if from_number:
                return from_number

            wait_time = time.time() - wait_start
            if wait_time > timeout:
                logger.warning(
                    f"From number pool exhausted for org {organization_id} "
                    f"config {telephony_configuration_id} after waiting "
                    f"{wait_time:.1f}s"
                )
                return None

            logger.debug(
                f"All from_numbers in use for org {organization_id} "
                f"config {telephony_configuration_id}, waited {wait_time:.1f}s, "
                "retrying..."
            )
            await asyncio.sleep(1)

    async def release_call_slot(self, workflow_run_id: int) -> bool:
        """
        Release concurrent slot and from_number when a call completes.
        Called by Twilio webhooks or workflow completion handlers.
        """
        slot_released = await call_concurrency.release_workflow_run_slot(
            workflow_run_id
        )

        # Release from_number back to its (org, telephony config) pool
        from_number_mapping = await rate_limiter.get_workflow_from_number_mapping(
            workflow_run_id
        )
        if from_number_mapping:
            fn_org_id, fn_number, fn_tcid = from_number_mapping
            fn_success = await rate_limiter.release_from_number(
                fn_org_id, fn_number, telephony_configuration_id=fn_tcid
            )
            if fn_success:
                await rate_limiter.delete_workflow_from_number_mapping(workflow_run_id)
                logger.info(
                    f"Released from_number {fn_number} for workflow run {workflow_run_id}"
                )

        return slot_released


# Global instance
campaign_call_dispatcher = CampaignCallDispatcher()
