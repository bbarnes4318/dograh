"""Combine runs into results.json / summary.md / CSVs and run the acceptance gate.

    python -m bench.report --corpus /secure/hopstt-corpus \
        --runs /secure/hopstt-runs/batch /secure/hopstt-runs/stream \
        --baseline cartesia --out /secure/hopstt-report

Engines: every run directory (``batch:<model>``, ``stream:<model>@<chunk>ms``)
plus ``imported:<name>`` for each transcript set found in the manifest's
``baseline`` map (e.g. existing Cartesia output -- a *baseline*, never ground
truth). Only verified references are scored. Imported baselines are scored only
on items that have that baseline text; a HopSTT request that failed is scored
as an empty hypothesis.

Reports contain anonymized aggregates only (opaque item ids and numbers, no
transcript text) unless ``--include-text`` is given, which additionally writes
``items_text.private.csv`` (owner-only) for local private review.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import time
from pathlib import Path

from .dataset import is_verified, load_manifest, manifest_path
from .metrics import (GateTargets, acceptance_gate, aggregate, compare_engines, metric_ci,
                      score_engine)
from .runs import find_runs, hypotheses_from_rows, load_run
from .safety import add_safety_args, prepare_output_dir, resolve_corpus, write_private


def collect_engines(items: list[dict], run_dirs: list[Path]) -> dict[str, dict]:
    """label -> {kind, meta, items (subset scored), hyps}"""
    engines: dict[str, dict] = {}
    by_id = {it["id"]: it for it in items}
    for d in run_dirs:
        meta, rows = load_run(d)
        label = meta.get("engine") or d.name
        if label in engines:
            label = f"{label}#{d.name}"
        hyps = hypotheses_from_rows(rows)
        subset = [by_id[i] for i in hyps if i in by_id]
        engines[label] = {"kind": meta.get("kind"), "meta": meta, "items": subset, "hyps": hyps, "dir": str(d)}
    names = sorted({k for it in items for k in (it.get("baseline") or {})})
    for name in names:
        subset = [it for it in items if name in (it.get("baseline") or {})]
        engines[f"imported:{name}"] = {"kind": "imported", "meta": {"engine": f"imported:{name}"},
                                       "items": subset,
                                       "hyps": {it["id"]: it["baseline"][name] for it in subset}}
    return engines


def latency_for_gate(meta: dict) -> dict | None:
    lat = meta.get("latency") or {}
    if "ttfp_ms" not in lat:
        return None
    return {"first_partial_ms": lat["ttfp_ms"], "finalize_to_final_ms": lat["finalize_to_final_ms"]}


def build_results(items: list[dict], engines: dict[str, dict], *, baseline: str | None,
                  candidates: list[str] | None, compare_pairs: list[tuple[str, str]],
                  targets: GateTargets, n_boot: int, seed: int) -> tuple[dict, dict]:
    scores = {name: score_engine(e["items"], e["hyps"]) for name, e in engines.items()}
    results: dict = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "note": "Anonymized aggregates only. No transcript text. Unverified references are excluded.",
        "n_verified_items": len(items),
        "n_verified_by_language": {l: sum(1 for i in items if i.get("language") == l) for l in ("en", "es")},
        "engines": {}, "comparisons": [], "gates": [],
    }
    for name, e in engines.items():
        agg = aggregate(scores[name])
        agg["ci"] = {m: metric_ci(scores[name], m, n_boot=n_boot, seed=seed)
                     for m in ("wer", "negative_flip_rate", "field_accuracy")}
        agg["kind"] = e["kind"]
        meta = e["meta"]
        for k in ("model", "chunk_ms", "frame_ms", "encoding", "sample_rate", "speed", "finalize_at",
                  "mode", "concurrency", "latency", "failures", "throughput_audio_s_per_s"):
            if k in meta:
                agg.setdefault("run", {})[k] = meta[k]
        results["engines"][name] = agg

    base_label = f"imported:{baseline}" if baseline and f"imported:{baseline}" in engines else baseline
    if base_label and base_label not in engines:
        print(f"warning: baseline {base_label} not found; gate skipped", file=sys.stderr)
        base_label = None
    cands = candidates or [n for n in engines if engines[n]["kind"] in ("batch", "stream")]
    pairs = [(c, base_label) for c in cands if base_label and c != base_label] + compare_pairs
    for a, b in pairs:
        if a not in scores or b not in scores:
            print(f"warning: cannot compare {a} vs {b} (unknown engine)", file=sys.stderr)
            continue
        cmp_ = compare_engines(scores[a], scores[b], n_boot=n_boot, seed=seed)
        results["comparisons"].append({"candidate": a, "baseline": b, **cmp_})
        if b == base_label and a in cands:
            kind = engines[a]["kind"]
            gate = acceptance_gate(cmp_, latency=latency_for_gate(engines[a]["meta"]), targets=targets,
                                   require_latency=(kind == "stream"))
            results["gates"].append({"candidate": a, "baseline": b, **gate})
    return results, scores


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


def _f(v, nd=4) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def _ci(ci: dict) -> str:
    if not ci or ci.get("value") is None:
        return "-"
    if ci.get("ci_low") is None:
        return _f(ci["value"])
    return f"{ci['value']:.4f} [{ci['ci_low']:.4f}, {ci['ci_high']:.4f}]"


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    keys: list[str] = []
    for r in rows:
        keys += [k for k in r if k not in keys]
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def write_outputs(out: Path, results: dict, scores: dict, engines: dict, items: list[dict],
                  include_text: bool) -> None:
    (out / "results.json").write_text(json.dumps(results, indent=2, default=str) + "\n")
    E = results["engines"]
    write_csv(out / "engines.csv", [{
        "engine": n, "kind": a["kind"], "n_items": a["n_items"], "n_missing": a["n_missing"],
        "ref_words": a["overall"]["ref_words"], "wer": a["overall"]["wer"],
        "wer_ci_low": a["ci"]["wer"]["ci_low"], "wer_ci_high": a["ci"]["wer"]["ci_high"],
        "sub": a["overall"]["sub"], "del": a["overall"]["del"], "ins": a["overall"]["ins"],
        "cer": a["overall"]["cer"], "field_accuracy": a["fields"]["accuracy"],
        "numeric_accuracy": a["fields"]["numeric_accuracy"],
        "negative_flip_rate": a["negation"]["negative_flip_rate"],
        "negative_to_affirmative_rate": a["negation"]["negative_to_affirmative_rate"],
        "affirmative_to_negative_rate": a["negation"]["affirmative_to_negative_rate"],
        "halluc_words_per_item": a["hallucination"]["words_per_item"],
        "halluc_rate_any": a["hallucination"]["rate_any_output"],
    } for n, a in E.items()])
    write_csv(out / "by_language.csv", [{"engine": n, "language": l, **b}
                                        for n, a in E.items() for l, b in a["by_language"].items()])
    write_csv(out / "by_tag.csv", [{"engine": n, "tag": t, **b} for n, a in E.items() for t, b in a["by_tag"].items()])
    write_csv(out / "fields.csv", [{"engine": n, "field_type": t, **b}
                                   for n, a in E.items() for t, b in a["fields"]["by_type"].items()])
    write_csv(out / "latency.csv", [{"engine": n, "metric": m, **s}
                                    for n, a in E.items() for m, s in ((a.get("run") or {}).get("latency") or {}).items()])
    cmp_rows = []
    for c in results["comparisons"]:
        for d in c["metrics"].values():
            cmp_rows.append({"candidate": c["candidate"], "baseline": c["baseline"], "n_items": c["n_items"], **d})
    write_csv(out / "comparisons.csv", cmp_rows)
    item_rows = []
    for n, sc in scores.items():
        for s in sc:
            item_rows.append({"engine": n, "id": s.id, "language": s.language, "tags": ";".join(s.tags),
                              "missing": s.missing, "ref_words": s.words.ref_len, "sub": s.words.sub,
                              "del": s.words.dele, "ins": s.words.ins, "hyp_words": s.hyp_words,
                              "fields": len(s.fields), "fields_correct": sum(f.correct for f in s.fields),
                              "neg_ref": s.neg_ref, "neg_flips": s.neg_flips})
    write_csv(out / "items.csv", item_rows)
    if include_text:
        by_id = {it["id"]: it for it in items}
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["engine", "id", "language", "reference", "hypothesis"])
        for n, e in engines.items():
            for iid, hyp in e["hyps"].items():
                if iid in by_id:
                    w.writerow([n, iid, by_id[iid].get("language"), by_id[iid].get("reference", ""), hyp or ""])
        write_private(out / "items_text.private.csv", buf.getvalue())
    (out / "summary.md").write_text(summary_markdown(results))


def summary_markdown(r: dict) -> str:
    E = r["engines"]
    L = ["# HopSTT benchmark summary", "",
         f"Generated {r['generated_at']}. {r['note']}", "",
         f"Verified items: {r['n_verified_items']} (EN {r['n_verified_by_language']['en']}, "
         f"ES {r['n_verified_by_language']['es']}). WER/CER are micro-averaged on normalized text; "
         "95% CIs are item-level bootstrap.", "",
         "## Engines", "",
         "| engine | items | WER [95% CI] | CER | S/D/I | critical-field acc | numeric acc | "
         "neg-flip rate [95% CI] | neg->aff | aff->neg | halluc words/item | halluc any | failed |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for n, a in E.items():
        o, ng, h = a["overall"], a["negation"], a["hallucination"]
        L.append(f"| {n} | {a['n_items']} | {_ci(a['ci']['wer'])} | {_f(o['cer'])} | {o['sub']}/{o['del']}/{o['ins']} | "
                 f"{_f(a['fields']['accuracy'])} | {_f(a['fields']['numeric_accuracy'])} | "
                 f"{_ci(a['ci']['negative_flip_rate'])} ({ng['negative_flips']}/{ng['n_negative_ref']}) | "
                 f"{ng['negative_to_affirmative']} | {ng['affirmative_to_negative']} | "
                 f"{_f(h['words_per_item'], 3)} | {_f(h['rate_any_output'], 3)} | {a['n_missing']} |")
    langs = sorted({l for a in E.values() for l in a["by_language"]})
    L += ["", "## WER by language", "", "| engine | " + " | ".join(langs) + " |", "|---|" + "---|" * len(langs)]
    for n, a in E.items():
        L.append(f"| {n} | " + " | ".join(_f((a["by_language"].get(l) or {}).get("wer")) for l in langs) + " |")
    tags = sorted({t for a in E.values() for t in a["by_tag"]})
    if tags:
        names = list(E)
        L += ["", "## WER by tag (n items in parentheses)", "", "| tag | " + " | ".join(names) + " |",
              "|---|" + "---|" * len(names)]
        for t in tags:
            cells = []
            for n in names:
                b = E[n]["by_tag"].get(t)
                cells.append(f"{_f(b['wer'])} ({b['n_items']})" if b else "-")
            L.append(f"| {t} | " + " | ".join(cells) + " |")
    ftypes = sorted({t for a in E.values() for t in a["fields"]["by_type"]})
    if ftypes:
        names = list(E)
        L += ["", "## Critical-field accuracy by type", "", "| field | " + " | ".join(names) + " |",
              "|---|" + "---|" * len(names)]
        for t in ftypes:
            cells = []
            for n in names:
                b = E[n]["fields"]["by_type"].get(t)
                cells.append(f"{_f(b['accuracy'])} ({b['correct']}/{b['n']})" if b else "-")
            L.append(f"| {t} | " + " | ".join(cells) + " |")
    lat_engines = [(n, a["run"]["latency"]) for n, a in E.items()
                   if (a.get("run") or {}).get("latency") and "ttfp_ms" in a["run"]["latency"]]
    if lat_engines:
        L += ["", "## Streaming latency (client clock, ms)", "",
              "TTFP = first speech frame sent -> first non-empty transcript (includes network, chunk "
              "buffering, inference, queue). finalize->final = `finalize` sent -> final (VAD delay excluded).", "",
              "| engine | TTFP p50/p95/p99 | finalize->final p50/p95/p99 | speech end->final p95 | "
              "revision rate (mean) | partial edit (mean) | failures |", "|---|---|---|---|---|---|---|"]
        for n, lat in lat_engines:
            t, f, s = lat["ttfp_ms"], lat["finalize_to_final_ms"], lat["speech_end_to_final_ms"]
            fails = (E[n]["run"].get("failures") or {})
            L.append(f"| {n} | {_f(t['p50'],0)}/{_f(t['p95'],0)}/{_f(t['p99'],0)} | "
                     f"{_f(f['p50'],0)}/{_f(f['p95'],0)}/{_f(f['p99'],0)} | {_f(s['p95'],0)} | "
                     f"{_f(lat['revision_rate']['mean'],3)} | {_f(lat['partial_edit_mean']['mean'],2)} | "
                     f"{fails.get('failed', '-')}/{fails.get('n_streams', '-')} |")
    if r["comparisons"]:
        L += ["", "## Paired comparisons (candidate - baseline, same items)", "",
              "| candidate | baseline | items | metric | candidate | baseline | diff [95% CI] |",
              "|---|---|---|---|---|---|---|"]
        for c in r["comparisons"]:
            for m, d in c["metrics"].items():
                if d.get("diff") is None:
                    continue
                L.append(f"| {c['candidate']} | {c['baseline']} | {d['n_units']} | {m} | {_f(d['a'])} | {_f(d['b'])} | "
                         f"{d['diff']:+.4f} [{_f(d['ci_low'])}, {_f(d['ci_high'])}] |")
    if r["gates"]:
        L += ["", "## Acceptance gate", ""]
        for g in r["gates"]:
            L.append(f"### {g['candidate']} vs {g['baseline']}: **{g['verdict']}**")
            L.append("")
            for name, c in g["checks"].items():
                L.append(f"- {name}: {c['status']} - {c['detail']}")
            L.append("")
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bench.report", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus")
    ap.add_argument("--manifest")
    ap.add_argument("--runs", nargs="+", default=[], help="run dirs, or parents of run dirs")
    ap.add_argument("--baseline", default="cartesia",
                    help="baseline engine: name in the manifest baseline map (-> imported:<name>) or a run label; 'none' to skip")
    ap.add_argument("--candidates", help="comma-separated engine labels to gate (default: every HopSTT run)")
    ap.add_argument("--compare", action="append", default=[], help="extra paired comparison 'A,B' (repeatable)")
    ap.add_argument("--bootstrap", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--min-items", type=int, default=200)
    ap.add_argument("--min-es-items", type=int, default=50)
    ap.add_argument("--min-silence-items", type=int, default=10)
    ap.add_argument("--wer-margin", type=float, default=0.01)
    ap.add_argument("--field-margin", type=float, default=0.01)
    ap.add_argument("--flip-margin", type=float, default=0.01)
    ap.add_argument("--first-partial-ms", type=float, default=250)
    ap.add_argument("--finalize-final-ms", type=float, default=300)
    ap.add_argument("--latency-stat", choices=["p50", "p95", "p99"], default="p95")
    ap.add_argument("--include-text", action="store_true",
                    help="ALSO write items_text.private.csv with reference/hypothesis text (local private review only)")
    ap.add_argument("--out", help="output directory (default $HOPSTT_BENCH_OUT/report-<ts>)")
    add_safety_args(ap)
    args = ap.parse_args(argv)

    corpus = resolve_corpus(args.corpus, allow_in_repo=args.allow_in_repo)
    items = [it for it in load_manifest(manifest_path(corpus, args.manifest)) if is_verified(it)]
    run_dirs = find_runs(args.runs)
    engines = collect_engines(items, run_dirs)
    if not engines:
        print("no engines found (no runs and no baseline transcripts)", file=sys.stderr)
        return 1
    targets = GateTargets(min_items=args.min_items, min_es_items=args.min_es_items,
                          min_silence_items=args.min_silence_items, wer_margin=args.wer_margin,
                          field_margin=args.field_margin, flip_margin=args.flip_margin,
                          first_partial_ms=args.first_partial_ms, finalize_to_final_ms=args.finalize_final_ms,
                          latency_stat=args.latency_stat)
    pairs = [tuple(x.split(",", 1)) for x in args.compare]
    baseline = None if args.baseline == "none" else args.baseline
    cands = [c.strip() for c in args.candidates.split(",")] if args.candidates else None
    results, scores = build_results(items, engines, baseline=baseline, candidates=cands, compare_pairs=pairs,
                                    targets=targets, n_boot=args.bootstrap, seed=args.seed)
    out = prepare_output_dir(args.out, prefix="report", allow_in_repo=args.allow_in_repo)
    write_outputs(out, results, scores, engines, items, args.include_text)
    for g in results["gates"]:
        print(f"GATE {g['candidate']} vs {g['baseline']}: {g['verdict']}")
        for reason in g["reasons"]:
            print(f"  - {reason}")
    print(f"report written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
