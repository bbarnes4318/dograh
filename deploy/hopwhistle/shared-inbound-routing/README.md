# Shared phone numbers and cross-workspace inbound AI routing

One pool of NetEnroll DIDs, split by percentage across AI agents that live in
**separate** workspaces (Final Expense, Real Estate, Holiday Voices), set
from NetEnroll AI Voice → **Inbound Routing**. Nothing is moved or copied:
each agent stays in its own workspace, and each call runs and is stored there.

This builds on what was already in the fork's `main` (routing groups, PR
2d6cd87; linked workspaces, ff2cbb4; screening gate, 3394368 / 9d4369d). It is
not a new routing system.

## How a call flows in production

```
caller ─► FracTEL ─► Hopwhistle FreeSWITCH ─► /api/v1/freeswitch/lookup
                         │  1. screenInboundCaller(ANI)  ← TCPA litigator screen, once
                         │  2. DID marked dograhCallback? → aiAgentBridge
                         ▼
              Dograh Asterisk, endpoint hopwhistle-callback ─► Stasis app "dograh"
                         │  3. inbound_admission: refuse any other endpoint (when the
                         │     owner's "answer only screened callers" setting is on)
                         │  4. number → owner's row (owner wins over copies)
                         │  5. routing group: one weighted (or sticky) pick
                         │  6. run created IN THE AGENT'S WORKSPACE, with that
                         │     workspace's ARI config, concurrency, quota, media
                         ▼
              Holiday Voices / Real Estate / Final Expense agent
```

The provider-webhook path (`/inbound/run`) is not used for these numbers. It
never routes across workspaces; a linked agent picked there is skipped and the
reason recorded.

## What each workspace sees

| | NetEnroll (owner) | Holiday Voices / Real Estate (linked) |
|---|---|---|
| Routing groups, percentages, numbers | edit | not visible (404) |
| Inbound Routing → Shared numbers | every owned number, the group controlling it, who it is shared with and their % | the numbers shared with it, owner, group, **its own** share and agents (read-only) |
| Calls its agents take | counted in the group's stats | its own runs, transcripts, recordings, analytics |
| Other workspaces' agents, callers, recordings, carrier credentials | — | never shown |
| Put its own agent on a shared DID | — | refused (409, "routed by Final Expense") |

"Shared with" is derived from the owner's routing group (a target among the
linked workspace's agents, 0% = paused). There is no second sharing list to
drift. Inbound sharing does **not** grant outbound caller-ID use: caller-ID
pools are untouched.

Linking stays operator-only (`scripts/link_workspaces.sql`); no API or UI
writes it.

## What changed in this PR

- **Shared-number inventory** — `GET /api/v1/inbound-routing-groups/shared-numbers`
  and the *Shared numbers* panel (`services/telephony/shared_numbers.py`).
- **Agents named with their workspace** — "Alex — Final Expense". Optional
  `self_label` in the link setting names the owner (`link_workspaces.sql -v self_label=...`).
- **No silent overwrites** — saving a group sends the version it loaded; if
  someone saved in between, 409 and the editor reloads. A linked workspace
  can't route a shared DID itself, and if a copy is routed anyway the owner's
  row wins on every Asterisk connection (it used to depend on which
  connection got the call).
- **Explicit, recorded fallback** — every skipped target is stored on the
  call's routing decision with its workspace and reason (archived, unlinked,
  no ARI config on this Asterisk app, webhook path); `changed_workspace` marks
  a call that ended in a different business than picked. Calls refused after
  routing (call limit, agent gone, errors) get a `call_refused` audit row.
- **Stats** — per agent: workspace, picked, calls, actual vs target,
  unavailable (with reasons), fallback calls; group: eligible calls,
  fallbacks and their reasons, cross-workspace fallbacks, refusals, and a
  plain warning about small samples and sticky callers.
- **Sticky fix** — a 10-digit caller ID and its +1 form now hash the same, so
  a repeat caller isn't sent to a different business because the caller ID
  arrived in another format. E.164 callers keep their current agent.
- No database migration. No change to screening, to numbers outside a
  routing group, or to outbound calling.

## Activation (needs the owner's go-ahead; nothing here runs by itself)

All commands run on the box (`root@178.156.223.97`), between campaigns:
restarting the Dograh API drops live AI calls. The UI rebuild drops nothing.

1. **See what is deployed and what exists** (read-only):

   ```bash
   docker inspect dograh-api-1 --format '{{.Config.Image}}'
   git -C /opt/dograh-src log -1 --oneline
   cat /opt/dograh-src/scripts/shared_routing_status.sql | docker exec -i dograh-postgres-1 \
     sh -lc 'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -v source_org=1'
   ```

   Section 1 lists each workspace's org id and ARI
   app; section 4 lists the real workflow ids and names. Note the Real Estate
   and Holiday Voices org ids, and the Holiday Voices "Shopping & Phone
   Orders" workflow id. Section 3 must be empty for every workspace you link.

2. **Deploy the API** (after this PR merges into `main`):

   ```bash
   git -C /opt/dograh-src pull && cd /opt/dograh-src/deploy/hopwhistle/cutover && bash update_api.sh
   ```

3. **Deploy the UI** (after the companion UI PR merges into
   `hopwhistle/customizations`):

   ```bash
   cd /opt/dograh-src/deploy/hopwhistle/cutover && bash update_ui.sh
   ```

4. **Link the workspaces** (operator; one-way; repeatable). For each linked
   workspace, with the org ids from step 1:

   ```bash
   cat /opt/dograh-src/scripts/link_workspaces.sql | docker exec -i dograh-postgres-1 \
     sh -lc 'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -v source_org=1 -v linked_org=<HOLIDAY_VOICES_ORG> -v label="Holiday Voices" -v self_label="Final Expense"'
   ```

   Repeat with `-v linked_org=<REAL_ESTATE_ORG> -v label='Real Estate'` if
   Real Estate is not linked yet (step 1, section 2 shows it). No restart:
   the next call reads it.

5. **Configure 50 / 25 / 25** — see below. Applies to the next call.

6. **Check**: re-run step 1 (section 5 shows the split and number count;
   section 8 shows where calls went). Call one shared DID from a cell phone a
   few times with sticky off, and confirm the Holiday Voices call appears in
   Holiday Voices → Agent Runs, not in Final Expense.

## Configure 50 / 25 / 25

In NetEnroll AI Voice (`aivoice.netenroll…`, the insurance workspace):

1. **Inbound Routing** → **New group** (or edit an existing group with the pencil).
2. Name: `Main AI Inbound Distribution`.
3. **Add agent…** → `Alex — Final Expense` (or the chosen Final Expense agent).
4. **Add agent…** → the Real Estate agent (`<name> — Real Estate`).
5. **Add agent…** → `Shopping & Phone Orders — Holiday Voices`.
6. Type `50`, `25`, `25`. The total must read **100%** (green) before Save works.
7. **Sticky by caller**: off = every call is picked independently by the
   weights; on = repeat callers return to the same agent (actual split
   drifts). Choose, then **Save** → confirm.
8. In **Numbers**, tick the telephony configuration holding the shared DIDs
   → **Attach selected**. The dialog shows how many numbers will move first;
   confirm.
9. Watch **Last 24 hours**: target vs actual per agent and workspace, plus
   fallbacks and refusals. With few calls the actual split can be far from
   25% by chance; that is expected.

To pause a business without losing its place, set it to 0% (the others must
still total 100%).

## Rollback

Any of these takes effect on the next call; calls in progress are untouched.

- **Stop splitting**: Inbound Routing → turn the group's **Active** switch
  off. Its numbers go back to their own inbound agent.
- **Stop sending calls to Holiday Voices only**: set its agent to 0% (or
  remove it) and save.
- **Unlink** (operator): the link command with `-v unlink=1`. Its agents
  are skipped immediately (recorded as "not linked") and its Shared numbers
  view empties.
- **Code**: `update_api.sh` / `update_ui.sh` with the previous commit, or
  the cutover kit's `rollback.sh`. No migration to undo.

## Tests

`api/tests/test_shared_inbound_routing.py` runs the three-workspace scenario
on the real test database and Redis (only Asterisk's HTTP calls and the
hosted quota check are stubbed). It covers 50/25/25 over 4,000 calls, two DIDs
in one group, edits applying to the next call, stale-save refusal, sticky
and non-sticky, a Holiday Voices call running and being stored in Holiday
Voices (its ARI config, media and run), an unchanged Final Expense number, an
archived agent, an unlinked workspace, a workspace missing its ARI config,
the call limit, stats, both inventory views, cross-tenant denial, owner-wins
on duplicate rows and the screening gate.
`api/tests/telephony/test_shared_numbers_unit.py` covers the pure pieces.
