# Local CI — `scripts/ci/run.sh`

Replaces the GitHub Actions checks (GitHub Actions no longer runs here):
`api-tests.yml`, `pre-pr-drift-check.yml` and `hopvoice-tests.yml`.

```bash
./scripts/ci/run.sh fast      # before every PR (~3-4 min)
```

One check at a time: `drift`, `api`, `hopvoice`. `./scripts/ci/run.sh clean`
removes containers a killed run left behind.

Needs **Python 3.13** (3.12 for Hopvoice), **Node 22** and **Docker**. The
`pipecat` submodule is fetched if missing; `.venv` is created/updated with
`scripts/setup_requirements.sh --dev`. Linux/macOS/WSL only (bash).

| exit | meaning |
|---|---|
| 0 | PASS |
| 1 | FAIL |
| 3 | INCOMPLETE — a check could not run here. **Not a pass.** |
| 130 | interrupted (containers are still cleaned up) |

Report: `.ci-reports/<time>-<profile>-<sha>/summary.md` (newest 20 kept).

## What runs

| check | what | was |
|---|---|---|
| `drift` | `ruff check --select I,F401` + `ruff format --check` on `api/`; ESLint `--fix-dry-run` on `ui/` (fails if `npm run fix-lint` would change a file or an error remains); OpenAPI spec regenerated and compared with `docs/api-reference/openapi.json` | `pre-pr-drift-check.yml` |
| `api` | the whole `api/tests` pytest suite against Postgres 17 + pgvector and Redis 7 (throwaway containers); **fails if any test is skipped** | `api-tests.yml` |
| `hopvoice` | Hopvoice install, compile, no-GPU tests; the only skip allowed is the GPU streaming test without `torch` | `hopvoice-tests.yml` |

The drift check is **read-only**: the workflow ran `scripts/format.sh`, which
rewrites files, and failed on the diff. Here the same rules run in check
mode, and the OpenAPI file is restored after comparing. To fix drift, run
`./scripts/format.sh` (and `python -m scripts.dump_docs_openapi`) and commit.

The environment is scrubbed (`env -i`) before anything runs, so API keys and
`DATABASE_URL` in your shell never reach a test. With `CI_FORBID_SECRETS=1`,
merely receiving one fails the run.

Not ported (release/publishing, not checks): `docker-image.yml`,
`release-automation.yml`, `release-deployment.yml`, `slack-announcements.yml`,
`pr-conventional-labeler.yml`.
