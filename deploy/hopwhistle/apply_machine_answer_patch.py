#!/usr/bin/env python3
"""Stop the campaign insights page counting machine-answered calls as humans.

Why this exists
---------------
The box's hand-patched `/opt/dograh-patches/reports.py` (mounted over
`api/routes/reports.py`) serves the campaign insights page. Its
`_classify_run` decides human / voicemail from the transcript alone and
ignores the `call_disposition` and `call_tags` the live pipeline recorded
when the voicemail detector or a call hygiene guard hung up. A machine call
whose transcript held no known voicemail phrase was counted as a human, and
its disposition (`voicemail_detected`) then showed in the human breakdown.

This patch is the same change as the repo's `api/routes/reports.py`: a
machine disposition (`voicemail_detected`, `answering_bot`) or machine-end
tag always makes the call a voicemail; offer lines read to a machine are not
transfers; machine hang-ups no longer use up the LLM judgment budget; and the
classification cache moves to `insights:v4:` (so every past day is reclassified
on its next load), with a stale cached "human" re-derived when the run's
disposition says machine.

Usage (run as root on the box)
------------------------------
    python3 apply_machine_answer_patch.py --dry-run
    python3 apply_machine_answer_patch.py
    # then reload the api (drops calls in progress -- run between campaigns):
    cd /opt/dograh && docker compose restart api

It refuses to change anything unless every anchor matches exactly once and
the patched file compiles. A timestamped backup of the original is kept next
to it.
"""

from __future__ import annotations

import argparse
import py_compile
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPORTS = Path("/opt/dograh-patches/reports.py")
MARKER = "_is_machine_answer"

EDITS = [
    # 1. cache version + the machine disposition/tag sets
    (
        '''_CLS_CACHE_PREFIX = "insights:v3:"
_CLS_CACHE_TTL = 60 * 60 * 24 * 30  # 30 days
''',
        '''_CLS_CACHE_PREFIX = "insights:v4:"
_CLS_CACHE_TTL = 60 * 60 * 24 * 30  # 30 days

# What the live call pipeline persisted when it hung up on a machine. These are
# the platform's own verdict (voicemail detector, call hygiene guards, blocked
# transfer) and are authoritative: a call carrying one never reached a person,
# whatever the transcript, the LLM judgment or the heuristics say. Keep in sync
# with the dispositions/tags passed to ``end_call_for_hygiene`` and the
# voicemail detector's ``EndTaskReason.VOICEMAIL_DETECTED``.
_MACHINE_DISPOSITIONS = frozenset({"voicemail_detected", "answering_bot"})
_MACHINE_TAGS = frozenset({
    "voicemail_detected", "answering_bot", "vm_keyword_guard", "vm_after_screener",
    "call_screener_no_pickup", "transfer_blocked_machine",
})
''',
    ),
    # 2. helpers
    (
        '''def _parse_turns(txt: str):
''',
        '''def _json_list(raw: Any) -> List[Any]:
    """A JSON array column as a list, whether the driver hands back text or a list."""
    if isinstance(raw, str):
        try:
            raw = _json.loads(raw)
        except Exception:
            return []
    return raw if isinstance(raw, list) else []


def _is_machine_answer(dispo: Optional[str], call_tags: Optional[List[str]]) -> bool:
    """True when the live pipeline itself ended the call on a machine."""
    return dispo in _MACHINE_DISPOSITIONS or any(
        t in _MACHINE_TAGS for t in (call_tags or []) if isinstance(t, str)
    )


def _parse_turns(txt: str):
''',
    ),
    # 3. classifier signature
    (
        '''def _classify_run(txt: str, dispo: Optional[str], nodes_visited: List[str], transfer_state: Optional[str], category_override: Optional[str] = None) -> Dict[str, Any]:
    """Classify one call transcript. Returns a JSON-serializable summary dict."""
    turns = _parse_turns(txt or "")
''',
        '''def _classify_run(txt: str, dispo: Optional[str], nodes_visited: List[str], transfer_state: Optional[str], category_override: Optional[str] = None, call_tags: Optional[List[str]] = None) -> Dict[str, Any]:
    """Classify one call transcript. Returns a JSON-serializable summary dict."""
    machine_answer = _is_machine_answer(dispo, call_tags)
    turns = _parse_turns(txt or "")
''',
    ),
    # 4. category: a machine hang-up always wins
    (
        '''    # category — VM phrases are high precision (win); else trust the LLM judgment; else heuristic
    if not turns or not asst:
        category = "no_answer"
''',
        '''    # category — a machine hang-up recorded by the pipeline always wins (the call
    # was answered, by a machine); else VM phrases are high precision; else trust
    # the LLM judgment; else heuristic
    if machine_answer:
        category = "voicemail"
    elif not turns or not asst:
        category = "no_answer"
''',
    ),
    # 5. an offer line read to a machine is not a transfer
    (
        '''    fired_transfer = dispo == "transfer_call" or any(p in low_full for p in _XFER_PHRASES)
    transfer_answered = transfer_state in ("terminated", "completed")
''',
        '''    # The transfer tool refuses to dial a buyer for a machine, so an offer line
    # the agent read to a mailbox is not an interested prospect.
    fired_transfer = not machine_answer and (
        dispo == "transfer_call" or any(p in low_full for p in _XFER_PHRASES)
    )
    transfer_answered = not machine_answer and transfer_state in ("terminated", "completed")
''',
    ),
    # 6. remember the signal in the cached classification
    (
        '''        "category": category,
        "result": result,
''',
        '''        "category": category,
        "machine_answer": machine_answer,
        "result": result,
''',
    ),
    # 7. read the tags
    (
        '''                   r.gathered_context->'nodes_visited' as nodes_visited,
                   r.campaign_id,
''',
        '''                   r.gathered_context->'nodes_visited' as nodes_visited,
                   r.gathered_context->'call_tags' as call_tags,
                   r.campaign_id,
''',
    ),
    # 8. keep the LLM budget for ambiguous calls
    (
        '''                targets = [r for r in to_classify if _needs_llm(texts.get(r["id"], ""))][:_MAX_LLM_PER_REQ]
''',
        '''                # A machine hang-up recorded by the pipeline is already decided;
                # keep the per-request LLM budget for the genuinely ambiguous calls.
                targets = [
                    r for r in to_classify
                    if _needs_llm(texts.get(r["id"], ""))
                    and not _is_machine_answer(r["dispo"], _json_list(r["call_tags"]))
                ][:_MAX_LLM_PER_REQ]
''',
    ),
    # 9. classify with the tags
    (
        '''            nodes = r["nodes_visited"]
            if isinstance(nodes, str):
                try:
                    nodes = _json.loads(nodes)
                except Exception:
                    nodes = []
            cls = _classify_run(texts.get(r["id"], ""), r["dispo"], nodes or [], r["transfer_state"], category_override=cats.get(r["id"]))
''',
        '''            cls = _classify_run(
                texts.get(r["id"], ""),
                r["dispo"],
                _json_list(r["nodes_visited"]),
                r["transfer_state"],
                category_override=cats.get(r["id"]),
                call_tags=_json_list(r["call_tags"]),
            )
''',
    ),
    # 10. re-derive a cached "human" whose run was a machine hang-up
    (
        '''            logger.warning(f"insights cache write failed: {e}")
''',
        '''            logger.warning(f"insights cache write failed: {e}")

    # ---- a machine hang-up recorded after a run was first classified
    # The cached classification can predate the run's final disposition/tags
    # (or the rule that reads them). A run the pipeline ended on a machine is
    # re-derived from its cached transcript so it can never stay counted as a
    # human, with no transcript refetch.
    restale = [
        r for r in completed_rows
        if r["id"] in cached
        and not cached[r["id"]].get("machine_answer")
        and _is_machine_answer(r["dispo"], _json_list(r["call_tags"]))
    ]
    if restale:
        pipe = redis.pipeline()
        for r in restale:
            cls = _classify_run(
                cached[r["id"]].get("transcript") or "",
                r["dispo"],
                _json_list(r["nodes_visited"]),
                r["transfer_state"],
                call_tags=_json_list(r["call_tags"]),
            )
            cached[r["id"]] = cls
            pipe.set(f"{_CLS_CACHE_PREFIX}{r['id']}", _json.dumps(cls), ex=_CLS_CACHE_TTL)
        try:
            await pipe.execute()
        except Exception as e:
            logger.warning(f"insights cache write failed: {e}")
''',
    ),
]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--reports", type=Path, default=REPORTS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    path: Path = args.reports
    if not path.exists():
        print(f"{path} not found")
        return 1
    text = path.read_text()
    if MARKER in text:
        print(f"{path.name}: already patched, nothing to do")
        return 0

    for index, (find, replace) in enumerate(EDITS, start=1):
        count = text.count(find)
        if count != 1:
            print(
                f"NOT APPLYING ANYTHING: anchor for edit {index} found {count} "
                f"times in {path.name}, expected once. Send me the file."
            )
            return 1
        text = text.replace(find, replace, 1)

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tmp:
        tmp.write(text)
    try:
        py_compile.compile(tmp.name, doraise=True)
    except py_compile.PyCompileError as e:
        print(f"NOT APPLYING ANYTHING: patched file does not compile:\n{e}")
        return 1
    finally:
        Path(tmp.name).unlink(missing_ok=True)

    if args.dry_run:
        print(f"{path.name}: would apply {len(EDITS)} edit(s); patched file compiles")
        print("Dry run -- nothing written.")
        return 0

    backup = path.with_name(
        f"{path.name}.bak-machine-answer-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    shutil.copy2(path, backup)
    path.write_text(text)
    print(f"{path.name}: applied {len(EDITS)} edit(s)  (backup: {backup.name})")
    print("Now reload the api:  cd /opt/dograh && docker compose restart api")
    return 0


if __name__ == "__main__":
    sys.exit(main())
