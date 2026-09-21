# Conversation eval harness

Change a global prompt today and you find out whether qualification rate moved
by running live calls — days later, at cost. This runs the real engine (nodes,
transitions, tools, extraction) over the text-chat path with a model playing
the caller, so the same question is answered in seconds.

## Run it

```bash
source venv/bin/activate && set -a && source api/.env && set +a
python -m evals.conversation --workflow-id 42 --organization-id 1 --user-id 1
```

Exit code is 0 when every persona passes, 1 otherwise — so it works as a CI
gate as-is.

## Gate a prompt change

```bash
# before
python -m evals.conversation ... --save baseline.json

# after your change
python -m evals.conversation ... --baseline baseline.json
```

Regressions are reported per persona rather than as an average: a change that
lifts three personas and breaks a fourth is a regression, not a wash.

## What gets scored

**Deterministic checks** (no model, never flake):

- the agent opened the call rather than leaving dead air
- it never said anything on the persona's forbidden list
- it didn't repeat itself, which is what a stuck agent does
- the call converted when it should have — and *didn't* when it shouldn't,
  because converting a hard no is pushiness, not performance
- the call reached a conclusion instead of running to the turn cap

**A rubric judge** reads the transcript and scores 0–10 against what a good
call with that persona looks like. Skip it with `--no-judge`; the deterministic
checks still run and still fail the build.

## Personas

`personas.py` ships a starting set — ready buyer, busy brush-off, price
objection, hard no, confused caller, wrong person, prompt probe. They are data;
add the ones your scripts actually lose people on. Each carries the behaviour
the caller model plays, the outcome that counts as success, and whether a good
call converts at all.

## Notes

- Runs are created as text-chat runs, annotated `source: conversation_eval`, so
  they're distinguishable from real traffic in reports.
- The caller and the judge use the organization's own configured LLM.
- Personas run sequentially: they share one org's model quota, and parallel
  bursts trip rate limits more often than they save wall-clock.
