# Read-only AI Voice inventory

`collect_inventory.py` records what is actually running. Its output fills the
"Live baseline record" in `docs/voice-improvements/implementation-status.md`.
It changes nothing: no container is started, stopped or exec'd into for
writing, and every database query runs inside `BEGIN READ ONLY ... ROLLBACK`.

## What it may print

The script decides what can be printed; nothing else ever appears in its
output.

| Category | Examples |
| --- | --- |
| Identifiers | image refs and ids, git commits, the image revision label, alembic revisions, provider and model ids, trunk/endpoint names, index names, mount destinations, `HOPWHISTLE_*` marker names |
| Counts and booleans | phone numbers with an inbound workflow, tools whose destination was rewritten for direct transfer, whether a cron entry mentions `push_runs` |
| Settings | every allowlisted variable as `set` / `unset`; values only for trunk names, policies and flags |
| Secret parity | `jwt_secret_parity`: `match`, `mismatch`, `missing` or `UNKNOWN:<reason>`, compared in memory |

It never prints:

- complete configuration JSON, environment dumps, compose files or cron
  command lines;
- phone numbers, URLs with credentials, or free text;
- secret values, or any hash of a secret.

Any value that is not a plain identifier becomes `<non-identifier>`. A final
check refuses to emit any phone-number-like or long free-text string. A
source that cannot be read becomes
`{"status": "UNKNOWN", "reason": "<fixed code>"}`. Raw error text is never
included.

## Run it

On the Dograh box, from a checkout of this repository:

```bash
sudo python3 deploy/hopwhistle/inventory/collect_inventory.py > /root/inventory-$(date +%F).json
```

If hoppwhistle runs on the same host, add its container and database:

```bash
sudo python3 deploy/hopwhistle/inventory/collect_inventory.py \
    --hoppwhistle-api-container hopwhistle-api \
    --hoppwhistle-psql '["docker","exec","-i","hopwhistle-postgres-dev","psql","-U","callfabric","-d","callfabric"]' \
    > /root/inventory-$(date +%F).json
```

If hoppwhistle runs on another host, compare the JWT secrets without either
value being printed or stored. The secret goes over the SSH pipe straight
into the comparison:

```bash
ssh <hoppwhistle-host> 'docker exec hopwhistle-api printenv AIVOICE_JWT_SECRET' \
  | sudo python3 deploy/hopwhistle/inventory/collect_inventory.py --peer-secret-stdin \
  > /root/inventory-$(date +%F).json
```

Options:

| Option | Default | Use it when |
| --- | --- | --- |
| `--compose-dir` | `/opt/dograh` | the compose project is elsewhere |
| `--api-container` | `dograh-api-1` | names differ (`docker ps`) |
| `--ui-container` | `dograh-ui-1` | names differ (`docker ps`) |
| `--asterisk-container` | `dograh-asterisk` | names differ (`docker ps`) |
| `--dograh-psql` | derived | as a JSON argv, when the default `docker compose exec -T postgres psql` with the user and database from the api's `DATABASE_URL` is not right |
| `--skip-db` | off | for a quick run without database access |

The derived psql command reads only the user and database name from
`DATABASE_URL`, never its password.

Review the JSON before sharing it. It is safe by construction, but you are the
last check.

## What remains unknown until it runs

Every live field in the status document stays `UNKNOWN` until this has been
run on the box and its output recorded. In particular:

- which image and Pipecat version are running;
- which files are mounted over the source;
- the database revision;
- the selected providers and models;
- whether any workflow uses GPT-Live;
- the transfer-destination behavior;
- the installed Asterisk blocks;
- JWT secret parity;
- whether call-quality ingestion and inbound callbacks are configured.
