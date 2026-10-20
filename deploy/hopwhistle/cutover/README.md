# hopwhistle-prod-ash: move api to the fork-built image

The box is documented as running the stock `dograhai/dograh-api:latest` image
with patch files bind-mounted over it from `/opt/dograh-patches`. Nobody has
checked the live box against this. Confirm it with
[`../inventory/collect_inventory.py`](../inventory/README.md) before relying on
it.

These scripts replace that setup with an image built from this fork, with no
patch mounts. Only the `api` container changes. postgres, redis, minio, the
`dograh-ui:voicestudio` UI and hoppwhistle's FreeSWITCH are never touched
(`--no-deps` everywhere).

What changed in Task 0A (voice-improvement program):

- **Migration head.** The target revision is read from the image being
  deployed, never from a constant. The previous scripts hard-coded
  `b7c41d9e52aa`, but `main` is now four migrations past the old base. Their
  own check would have failed after migrating and then auto-rolled-back.
- **Mount removal.** Mounts are only removed when the behavior they carry is
  known, present in the target image, and recreated through explicit settings
  where it was patch-only (`mount_manifest.json`). An unknown mount blocks the
  plan.
- **Failure handling.** A failure after migrating no longer rewrites
  `alembic_version` or starts the old image on its own. It holds and offers
  explicit recovery choices.

## Files

| File | What it does |
| --- | --- |
| `cutover_plan.py` | Read-only planner. Subcommands: `graph`, `plan`, `fingerprint`, `diff`, `decide`, `check`, `verify`. It only writes into the state directory. |
| `cutover_lib/` | The planner's logic, standard library only, tested offline by `api/tests/test_cutover_*.py` |
| `mount_manifest.json` | Compatibility manifest for every known patch mount |
| `common.sh` | Shell helpers and `run_switch` (backups, atomic override swap, recreate, verify, failure handling) |
| `1_build_image.sh` | Builds `hopwhistle/dograh-api:<sha>` on half the cores and checks its migration graph statically |
| `2_cutover.sh` | First switch from patch mounts to the fork image |
| `update_api.sh` | Later switches (box already on a fork image) |
| `recover.sh` | Explicit recovery choices after an unfinished switch |
| `rollback.sh` | Shorthand for `recover.sh image-rollback` |
| `3_set_call_hygiene.sh`, `day_one_review.sh`, `update_ui.sh` | Unchanged in purpose |

## How a switch works

```
plan (read-only) -> confirm -> backups -> fingerprint re-check -> override swap -> recreate api -> wait -> verify
```

1. **Plan.** `cutover_plan.py plan` writes everything to
   `/opt/dograh-backups/<ts>/` (mode 700):
   - **Target graph.** The target image's migrations are read with
     `docker create` (entrypoint replaced, no network, never started) and
     `docker cp`, then parsed with `ast`. Nothing is imported or executed, so
     this cannot run the image's startup `alembic upgrade head`.
   - **Running graph.** The running container's migrations are read the same
     way.
   - **Database revisions.** The complete `alembic_version` row set, read in a
     `READ ONLY` transaction.
   - **Blocking rules.** A blocker stops the plan with nothing changed. It
     blocks on:
     - a missing or empty version table, or more than one row;
     - a database revision that differs from the running image's head;
     - a revision unknown to the target (database ahead or diverged);
     - a running image with migrations the target lacks;
     - a target with more than one head, a dangling parent, a cycle or branch
       labels;
     - more than `--max-pending` (20) pending migrations;
     - a pending migration that is not purely additive, until you pass
       `--allow-migration <rev>`.
   - **Mounts and settings.** Every api mount is checked against
     `mount_manifest.json` (next section). The settings that replace
     patch-only behavior are derived, and the new override is generated.
   - **Output.** `plan.txt` and `plan.json` are redacted: setting values are
     shown only for trunk names, policies and flags, everything else as
     `<set>`, and phone-number-like runs as `<number>`.
     `override.new.yaml` (mode 600) is the only file with values.
2. **Confirm.** The plan is printed. Type `CUTOVER` (or `UPDATE`) to continue.
   `ASSUME_YES=1` skips this.
3. **Backups.** `pg_dump -Fc` (verified readable), the current override, and a
   `hopwhistle/dograh-api-rollback:<ts>` tag of the running image. A marker,
   `/opt/dograh-backups/in-progress`, now points at the state directory. It
   survives a killed shell or a reboot, and every script refuses to start a
   new switch while it exists.
4. **Fingerprint.** The override, database revisions, target image id and
   running image id are compared with the plan. Any drift aborts.
5. **Override swap.** The generated override is validated with
   `docker compose config`, then moved into place atomically.
6. **Recreate.** `api` is recreated, and pending migrations run on start.
7. **Wait.** Healthy within `HEALTH_TIMEOUT_SECONDS` (420). An exited or
   restarting container counts as failed. A container that is still running
   is **never** stopped, because it may be in a long
   `CREATE INDEX CONCURRENTLY`.
8. **Verify.** `cutover_plan.py verify` checks:
   - the database is exactly on the target head;
   - api runs the planned image id;
   - no removed mount is still mounted;
   - each derived setting in the container matches the override.

## Rollout procedure

All scripts run as root on the box from `/opt/dograh-src/deploy/hopwhistle/cutover`.
Run them inside `tmux` or `screen`, so an SSH drop cannot interrupt a switch.

| When (ET) | Step | Drops calls? |
| --- | --- | --- |
| Any time | `sudo python3 ../inventory/collect_inventory.py > /root/inventory-before.json`, then record the live baseline (`docs/voice-improvements/implementation-status.md`) | no |
| Sun 21:30 | `sudo bash 1_build_image.sh <full sha>`: build on half the cores, 20-40 min | no |
| Mon 07:00 | **Dry run.** `sudo bash 2_cutover.sh <short-sha>`, then answer anything but `CUTOVER`. Read `plan.txt`, then resolve every `BLOCKED` line (below) | no |
| Mon 07:30 | `sudo bash 2_cutover.sh <short-sha> [--ack-mount ...] [--set ...]`, guards **off** | yes, api restarts |
| Mon 07:35-08:30 | Parity checklist below, before any campaign | no |
| Mon | One normal calling day on the new image | |
| Tue 07:30 | `3_set_call_hygiene.sh on`, between campaigns | yes, api restarts (~30 s) |
| Wed 08:00 | `day_one_review.sh <Tuesday's date>` | no, read-only |

Typical blockers and what to do:

- **`unknown mount`.** Run `python3 cutover_plan.py diff --target-image <img> <target>`
  (the output is redacted). Decide what the file carries. Add a manifest
  entry, with evidence the target image must contain and, if needed, a
  derivation in `cutover_lib/mounts.py`. Rebuild the image if the behavior
  is missing from source.
- **`recognised heuristically` / `content not recognised`.** Review the same
  diff. If the mounted file carries nothing beyond the listed behaviors,
  re-run with `--ack-mount <target>=<sha256 printed in the plan>`. An
  acknowledgement only matches that exact file content.
- **`... nor a single literal` (a derivation failed).** Supply the value
  explicitly with `--set NAME=VALUE`. It shows in the plan as an operator
  value.
- **A non-additive pending migration.** Read the migration, then pass
  `--allow-migration <rev>`. After such a migration, no image rollback is
  possible without a database restore.

## Mount compatibility

`mount_manifest.json` lists every patch mount we know of: the hoppwhistle
`deploy/dograh/*` kits, this repo's `deploy/hopwhistle/apply_*_patch.py`, and
the box-local edits the old `2_cutover.sh` read.

For each mount target it lists:

- the recognisable variants (regexes that must all match the mounted file);
- the behaviors each variant carries;
- for each behavior, its status, where it lives in source, and the evidence
  checked **inside the target image**.

| Mounted file | Patch-only behavior | Preserved as |
| --- | --- | --- |
| `ari/provider.py`, ari-trunk V1 | trunk from `DOGRAH_ARI_TRUNK` (patch default `fractel`), transfer trunk from `DOGRAH_ARI_TRANSFER_TRUNK`, any `Tech/` (incl. `Local/`) dialled as written | `ARI_PJSIP_DEFAULT_TRUNK` (set explicitly, `fractel` when the patch env was unset), `ARI_PJSIP_TRANSFER_TRUNK`, `ARI_DIAL_ANY_TECHNOLOGY=true` |
| ari-trunk V2 / V3 | dial prefix, NANP number format | `ARI_DIAL_PREFIX`, `ARI_DIAL_FORMAT` |
| caller-trunk V1 | per-caller-ID trunk and format | `ARI_CALLER_TRUNKS` |
| box-local `@<trunk>` literals | hard-coded trunks | `ARI_PJSIP_DEFAULT_TRUNK` / `ARI_PJSIP_TRANSFER_TRUNK` (review + ack) |
| box-local transfer caller ID literal | hard-coded transfer caller ID | `ARI_TRANSFER_DEFAULT_CALLER_ID` (review + ack) |
| `campaign_call_dispatcher.py`, hoppwhistle kit | **every** live lead to one shared DID (`DOGRAH_TRANSFER_DESTINATION` or a literal) | `CAMPAIGN_TRANSFER_DESTINATION_POLICY=forced_default` + `CAMPAIGN_DEFAULT_TRANSFER_DESTINATION` |
| box-local dispatcher with fallback literal | lead, campaign, then a literal | `CAMPAIGN_TRANSFER_DESTINATION_POLICY=context_first` + `CAMPAIGN_DEFAULT_TRANSFER_DESTINATION` (review + ack) |
| everything else (state caller ID, rate limiter, area codes, transfer-duration hotfix, Fish TTS, Inception, call duration, insights, runs page) | none: already in source | evidence checked in the image |

The transfer-destination policy is **not** chosen by these scripts. They read
which variant is mounted and carry its behavior over exactly. The source
default (`context_first`, unset policy) is unchanged for every other
installation.

The ARI settings are implemented in
`api/services/telephony/providers/ari/dialing.py`. With them all unset,
dialing is identical to before. `api/tests/test_ari_dialing.py` checks the
port against the patch kits' code over a grid of numbers and settings.

**Limits.**

- Recognition is by kit markers and patterns. A mounted file can carry an
  edit that no marker reveals, so review `cutover_plan.py diff` for any file
  you did not produce with a kit.
- The planner also blocks when another service (a worker built from the api
  image) mounts over `/app/api`, and on override syntax it cannot rewrite
  safely (long-form volumes, inline lists, tabs).

## Failures and recovery

Image rollback and database recovery are separate decisions:

- The previous **image** can only start if the database is where that image
  left it. Its entrypoint runs `alembic upgrade head` and exits on a revision
  it does not know.
- The **database** is never downgraded and never restored automatically.

### What happens automatically

`handle_switch_failure` in `common.sh` asks `cutover_plan.py decide`. The
only automatic actions are:

| Failure point | Automatic action |
| --- | --- |
| Before the override swap | none (nothing changed) |
| After the swap, before `api` was recreated | restore the previous override file (the old container is still running) |
| After recreate, **and** the database is provably untouched | restore the previous override and image, start it, and confirm its revision |

"Provably untouched" means all of:

- the database is on the same single revision;
- the first pending migration is fully transactional;
- there are no invalid indexes;
- the new image never reported healthy, so it cannot have written rows the
  old code cannot read.

Every other failure **holds**:

| State | What the scripts do |
| --- | --- |
| New image healthy and compatible, a later check failed | keep it running |
| Still starting (e.g. a long index build) | leave it running |
| Crashed after migrating, partially or fully | stop the crashed container so a restart policy cannot loop |
| Database unreadable | nothing |

In each held case the scripts print the reasons and the allowed choices, and
the in-progress marker stays until you resolve it.

Partial states this covers:

- **Intermediate revision.** Migrations run one transaction each
  (`transaction_per_migration=True`), so a failure can leave the database
  between the old and new head.
- **Schema change without a revision change.** `b7c41d9e52aa` and
  `c3f9a1d7e4b2` run `CREATE INDEX CONCURRENTLY` outside a transaction. A
  failure there can leave committed schema, or an INVALID index, without
  moving `alembic_version`.
- **Rows the old code cannot read.** `9d3e1a7c5b20` and `a4c8e2f61b93` add
  `tool_category` values. Once the new image has written a `play_audio` or
  `send_sms` tool, the old code cannot load that row.

### Recovery choices (`recover.sh`)

```bash
sudo bash recover.sh status                  # read-only: revisions, container, invalid indexes, new-enum rows, allowed choices
sudo bash recover.sh hold                    # stop api, change nothing else
sudo bash recover.sh resume-forward          # start the NEW image again; it finishes the idempotent migrations
sudo bash recover.sh image-rollback          # start the PREVIOUS image; only if the DB is untouched
sudo bash recover.sh image-rollback --stamp-old-head
sudo bash recover.sh restore-db              # prints the manual restore procedure; runs nothing
sudo bash recover.sh release                 # clear the marker after resolving by hand
```

- **`resume-forward`** is the usual answer after a partial migration, because
  the new image understands every revision in its graph.
  - It is refused while invalid indexes exist. Drop them first with
    `DROP INDEX CONCURRENTLY <name>`; `IF NOT EXISTS` would otherwise skip
    rebuilding them.
  - It is refused when the database revision is unknown to the new image.
- **`--stamp-old-head`** runs the previous image on the forward schema. It
  sets `alembic_version` back to the old head after you type
  `STAMP <revision>`; the additive schema stays and the old code ignores it.
  It is refused unless:
  - every forward migration is additive or a pure enum-value addition;
  - no invalid index exists;
  - no row uses a new enum value.
- **`restore-db`** is for when neither forward nor back is acceptable. It
  destroys everything written since the backup. It restores into a scratch
  database first, then runs `image-rollback`. It is never automated.

## Tested

**Offline unit tests** (run by `api/tests`; see the PR for the commands):

- `test_cutover_migrations.py`: real repository graph, multiple heads, dangling
  parents, duplicates, cycles, branch labels, risk classification, parsing
  never executes code
- `test_cutover_plan.py`: migration mismatch, database ahead/diverged, multiple
  version rows, missing table, non-descendant target, too many pending,
  non-additive migration gate, shadowing services, unknown mounts, redaction,
  fingerprint
- `test_cutover_recovery.py`: every failure phase, partial migration,
  non-transactional first migration, served-traffic guard, invalid indexes,
  stamp-back preconditions
- `test_cutover_mounts.py`: every behavior's evidence present in current
  source, V1-V3/caller-trunk derivations, forced shared DID, heuristic
  variants need acknowledgement, unknown/unreadable mounts, missing evidence,
  exclusive variants, operator overrides
- `test_cutover_compose.py`: override parsing and minimal rewriting
- `test_ari_dialing.py`: dialing equivalence with the patch kits; transfer
  policy

**Sandbox end-to-end** (not committed).

- **Setup.**
  - The real scripts ran against a fake `docker` binary.
  - The database was a real PostgreSQL 16 + pgvector instance.
  - "Starting" the new image ran the real alembic migrations.
  - The old image was modelled as the migration subset up to `00b0201ad918`.
- **Results.**

  | Scenario | Result |
  | --- | --- |
  | Clean cutover | DB on the image-derived head, mounts removed, settings derived, verified |
  | Failure mid-migration (DB left on `9d3e1a7c5b20`) | held, image rollback refused, `resume-forward` completed and verified |
  | Same failure, stamp-back with a `play_audio` row present | refused |
  | Stamp-back, wrong confirmation | aborted |
  | Stamp-back, confirmed | previous image back, column kept |
  | Second cutover after that | succeeded (migrations re-ran idempotently) |
  | New image exits before migrating, first pending migration transactional | automatic image rollback, DB unchanged |
  | Same, but first pending migration non-transactional | held |
  | Unknown mount | blocked, nothing changed (no backup, no marker) |
  | Container never healthy | left running, held |
  | SIGTERM during the wait | trap ran, held, marker kept, `recover.sh status` correct |

**Not tested.**

- Real Docker or Compose.
- The real box's override and mounted files.
- A real pg_dump of production size.
- **No live validation has been performed.**

## Parity checklist (after the switch, guards off, before any campaign)

Tick every line. If anything fails, run `sudo bash recover.sh status` and
choose. If the database is untouched, `recover.sh image-rollback` brings the
previous setup back. Then send the output of
`docker compose logs --tail 300 api`.

- [ ] **Transfer through hoppwhistle.**
  - Run a one-lead test campaign to your own phone and say yes to the
    transfer. A live agent answers through hoppwhistle.
  - The destination matches the transfer policy shown in the plan:
    `forced_default` means the shared DID, whatever the lead or campaign
    says.
  - Stay connected past the workflow's max call duration. The call must not
    drop (transfer-duration hotfix).
- [ ] **Fish TTS.** On that call, Alex's voice is your Fish voice (not a
      default voice), at normal speed and volume, with no reading of markup
      or phoneme tags.
- [ ] **State caller ID.**
  - The caller ID on your phone is from the pool.
  - With `state_cid_policy` prefer or strict, it is a number from your
    phone's state.
  - Check the run's `initial_context` for `caller_id_state` /
    `caller_id_state_match`.
- [ ] **Reports.** The Campaign Insights page and the run report CSV load, and
      today's test call shows a non-zero duration and its direction.
- [ ] **ARI trunk.**
  - Outbound calls leave on the trunk shown in the plan's
    `ARI_PJSIP_DEFAULT_TRUNK`: look for `PJSIP/...@<trunk>` in
    `docker compose logs api | grep PJSIP/`.
  - With a dial prefix or format, the dialled number is written the same way
    as before the switch.
  - A direct-transfer `Local/` destination connects when
    `ARI_DIAL_ANY_TECHNOLOGY` was derived.
  - A no-answer call frees its caller ID (the pool doesn't shrink over the
    morning).
- [ ] `docker compose logs --since 30m api | grep -iE "error|traceback"`
      shows nothing new compared with a normal day.

Then run one normal calling day on the new image with the guards still off.

## Enable the guards (Tuesday, between campaigns)

```bash
sudo bash 3_set_call_hygiene.sh on
```

`3_set_call_hygiene.sh off` is the kill switch. It takes effect in ~30 s. The
TTS markup scrub stays on either way.

## Day-one review (Wednesday morning)

```bash
sudo bash day_one_review.sh <tuesday YYYY-MM-DD> 2026-09-25 https://<your-dograh-ui-host>
```

It is read-only (one `BEGIN READ ONLY ... ROLLBACK`). It prints the report and
saves a copy to `/opt/dograh-backups/reviews/`:

1. calls by `call_disposition`
2. counts for each guard tag: `vm_keyword_guard`, `vm_after_screener`,
   `call_screener_no_pickup`, `answering_bot`, `no_speech_dead_air`,
   `closing_line_watchdog`, `llm_markup_leak`, `transfer_blocked_machine`
3. transfer attempts, transfers connected, transfer rate, and average
   connected seconds, compared with 9/25
4. 30 random `vm_keyword_guard` calls, each with a run link (the page plays
   the recording), the recording key and the transcript. Listen for real
   people who got hung up on.

## Later updates

Once the box runs a fork-built image (between campaigns; api restarts):

```bash
git -C /opt/dograh-src pull
cd /opt/dograh-src/deploy/hopwhistle/cutover
sudo bash update_api.sh            # fork's main; or: sudo bash update_api.sh <git-sha> [planner options]
```

It builds, plans, confirms, backs up, switches and verifies, with the same
blocking rules and failure handling as `2_cutover.sh`. The expected head
always comes from the newly built image.
