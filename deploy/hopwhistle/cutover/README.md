# hopwhistle-prod-ash: move api to the fork-built image

The box runs the stock `dograhai/dograh-api:latest` (built 2026-07-22) with
20 files bind-mounted over it from `/opt/dograh-patches` and
`/opt/dograh-asterisk`. Every one of those patches is now in the fork (Fish
TTS, state-matched caller ID, ARI trunk + transfer caller ID, ARI no-answer
slot release, transfer-duration hotfix, campaign insights, ...), so the box
can run an image built from the fork with **no mounts**.

Only the `api` container changes. postgres, redis, minio, the
`dograh-ui:voicestudio` UI and hoppwhistle's FreeSWITCH are never touched
(`--no-deps` everywhere). The database gains one nullable column
(`workflow_runs.transcript_text`, migration `b7c41d9e52aa`).

## Schedule

| When (ET) | Step | Drops calls? |
| --- | --- | --- |
| Sun 21:30 | `1_build_image.sh <sha>`: build on half the cores, 20-40 min | no |
| Mon 07:30 | `2_cutover.sh <short-sha>`: backups + switch, guards **off** | yes, api restarts (~30 s) |
| Mon 07:35-08:30 | Parity checklist below, before any campaign | no |
| Mon | One normal calling day on the new image | |
| Tue 07:30 | `3_set_call_hygiene.sh on`, between campaigns | yes, api restarts (~30 s) |
| Wed 08:00 | `day_one_review.sh <Tuesday's date>` | no, read-only |

If your campaigns or transfer agents run outside these hours, move the
build to any window with no live transfers. Move the cutover and the enable
step to a gap between campaigns, with one agent available for the test
transfer.

All scripts run as root on the box, from this directory in the fork
checkout (`/opt/dograh-src/deploy/hopwhistle/cutover`).

## 1. Build (Sunday night)

```bash
sudo bash 1_build_image.sh <full git sha of the fork's main>
```

- Refuses to start with under 20 GB free in `/var/lib/docker`.
- Builds in a dedicated BuildKit container pinned to CPUs
  `0-$(( $(nproc)/2 - 1 ))`. `docker build --cpuset-cpus` does nothing
  under BuildKit, which this Dockerfile needs, so the pin goes on the builder
  container, and the script checks it took effect.
- Only adds the image tag `hopwhistle/dograh-api:<short-sha>`. Nothing
  running changes.

## 2. Cutover (Monday, before campaigns)

```bash
sudo bash 2_cutover.sh <short-sha>
```

1. Preflight: checks the image exists, the DB is on `00b0201ad918`, and
   there's room for the dump. Reads the trunk, transfer caller ID and default
   transfer destination out of the currently mounted files, so they never
   live in this public repo.
2. Asks before continuing if any call looks active.
3. Backups go to `/opt/dograh-backups/<timestamp>/` (mode 700; `latest` links
   to the newest):
   - `dograh.dump`: `pg_dump -Fc` of the dograh DB, verified readable
   - `docker-compose.override.yaml`: the current file, all 20 mounts
   - `rollback.env`: the exact pre-cutover image id, also tagged
     `hopwhistle/dograh-api-rollback:<timestamp>` so a later `docker compose
     pull` can't lose it
4. Writes the new override:
   - `image: hopwhistle/dograh-api:<sha>`
   - no volumes
   - `CALL_HYGIENE_ENABLED: "false"`
   - `DOGRAH_STATE_CID_POLICY` kept as it was
   - `ARI_PJSIP_DEFAULT_TRUNK`, `ARI_TRANSFER_DEFAULT_CALLER_ID` and
     `CAMPAIGN_DEFAULT_TRANSFER_DESTINATION` from step 1

   Validates it with `docker compose config`.
5. Recreates api, which applies the one migration on start. Waits for
   healthy and checks the DB is on `b7c41d9e52aa`.

If step 5 fails for any reason, `rollback.sh` runs automatically.

## Rollback (any time)

```bash
sudo bash rollback.sh                          # newest backup
sudo bash rollback.sh /opt/dograh-backups/<ts> # a specific one
```

1. Stops api.
2. Sets `alembic_version` back to `00b0201ad918`. The old image runs
   `alembic upgrade head` under `set -e`; without this it exits with *Can't
   locate revision identified by 'b7c41d9e52aa'*. The nullable
   `transcript_text` column stays; the old code ignores it.
3. Restores the backed-up override with all 20 mounts.
4. Re-points the old image tag at the exact pre-cutover image if anything
   moved it, and starts api with `--pull never`.
5. Waits for healthy, then checks the DB version and that the running image
   id matches the pre-cutover one.

No data is lost by rolling back, so the pg_dump is not restored. It is for
disasters only:
`docker compose exec -T postgres pg_restore -U postgres -d <db> --clean < dograh.dump`,
and only with the api stopped.

Cutting over again after a rollback is safe: the migration uses
`ADD COLUMN IF NOT EXISTS`.

### Tested on a copy of the box

Tested in a sandbox with:

- this compose file and the box's exact override
- the 20 patched files at the same `/opt/...` paths
- upstream `dograh-api:1.43.0`, which has the same 93 migrations and head as
  the box's image, tagged as `dograhai/dograh-api:latest`

Every scenario ended healthy on the expected image, mounts and migration:

| Scenario | Result |
| --- | --- |
| Cutover | healthy on `b7c41d9e52aa`, 0 mounts, guards off |
| Old image started on the new DB without the fix | reproduced *Can't locate revision*, container exited |
| `rollback.sh` from that broken state | healthy, 20 mounts, `00b0201ad918` |
| Cutover with an image that fails its migration | automatic rollback, healthy on the old image |
| Second cutover after a rollback | healthy (idempotent migration) |
| `latest` re-tagged to another image before rollback | rollback re-pinned it, running the pre-cutover image id |
| Restore `dograh.dump` into a scratch DB | all tables and rows present |

## Parity checklist (Monday, guards off, before any campaign)

Tick every line. Any failure means you run `rollback.sh`, then send me the
output of `docker compose logs --tail 300 api`.

- [ ] **Transfer through hoppwhistle.** Run a one-lead test campaign to your
      own phone. Say yes to the transfer. A live agent answers through
      hoppwhistle. Stay connected past the workflow's max call duration: the
      call must not drop (transfer-duration hotfix).
- [ ] **Fish TTS.** On that call, Alex's voice is your Fish voice (not a
      default voice), at normal speed and volume, with no reading of markup
      or phoneme tags.
- [ ] **State caller ID.** The caller ID on your phone is from the pool. If
      you run with `state_cid_policy` prefer/strict, it is a number from your
      phone's state. Check the run's `initial_context` for `caller_id_state`
      / `caller_id_state_match`.
- [ ] **Reports.** The Campaign Insights page and the run report CSV load,
      and today's test call shows a non-zero duration and its direction.
- [ ] **ARI trunk.** Outbound calls leave on the trunk (`PJSIP/...@<trunk>`
      in `docker compose logs api | grep PJSIP/`), and a no-answer call
      frees its caller ID (the pool doesn't shrink over the morning).
- [ ] `docker compose logs --since 30m api | grep -iE "error|traceback"`
      shows nothing new compared with a normal day.

Then run one normal calling day on the new image with the guards still off.

## Enable the guards (Tuesday, between campaigns)

```bash
sudo bash 3_set_call_hygiene.sh on
```

`3_set_call_hygiene.sh off` is the kill switch; it takes effect in ~30 s.
The TTS markup scrub stays on either way.

## Day-one review (Wednesday morning)

```bash
sudo bash day_one_review.sh <tuesday YYYY-MM-DD> 2026-09-25 https://<your-dograh-ui-host>
```

Read-only (one `BEGIN READ ONLY ... ROLLBACK`). Prints and saves to
`/opt/dograh-backups/reviews/`:

1. calls by `call_disposition`
2. counts for each guard tag: `vm_keyword_guard`, `vm_after_screener`,
   `call_screener_no_pickup`, `answering_bot`, `no_speech_dead_air`,
   `closing_line_watchdog`, `llm_markup_leak`, `transfer_blocked_machine`
3. transfer attempts, transfers connected, transfer rate, and average
   connected seconds, compared with 9/25
4. 30 random `vm_keyword_guard` calls, each with a run link (the page plays
   the recording), the recording key and the transcript. Listen for real
   people who got hung up on.

9/25 durations are only non-zero where the call-duration backfill was run, so
check the baseline's `connected` count before comparing averages.
