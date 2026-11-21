# HopSTT benchmark corpus

The corpus is built from **real call recordings**, which contain insurance and
health PII (names, dates of birth, phone numbers, conditions, medications).
It never enters any git repository, ticket, chat or shared drive. The tools
enforce part of this. The rest is procedure, described below.

## 1. Access control and storage

- **Location:** one directory on an encrypted volume of the benchmark host (or
  the GPU host), outside any git working tree, for example
  `/secure/hopstt-corpus`. Point the tools at it with `--corpus` or
  `HOPSTT_EVAL_CORPUS`. Every tool refuses a corpus, export, review sheet or
  output path that is inside a git working tree. `--allow-in-repo` exists only
  for synthetic fixtures.
- **Permissions:** the corpus and run directories are created `0700`. The
  manifest, review sheet, id map, salt, hypotheses and cut audio are `0600`.
  Only the people named as benchmark reviewers get an account on that host.
- **What is private:** everything under the corpus, and every run directory
  (`items.jsonl` holds hypothesis text), the id map, the salt, and
  `items_text.private.csv` (written only with `report --include-text`).
- **What may be shared:** `report`'s `summary.md`, `results.json` and CSVs, and
  `capacity`'s outputs. They hold aggregates, opaque ids and numbers, never
  transcript text. Review them before sharing anyway: a tag with one item
  combined with its WER says little, but do not attach item-level CSVs to a
  public place.
- **Ids:** items get opaque ids `hs_<16 letters>`, a salted SHA-256 of
  (call id, recording, channel, segment). File names are `audio/<id>.wav`.
  Call ids and phone numbers never appear in ids or file names, and `validate`
  rejects any id or file name that contains 7 or more consecutive digits. The
  salt (`SALT.private` or `$HOPSTT_ID_SALT`) and `id_map.private.jsonl` (id →
  call id, recording, channel, times) let an authorised reviewer go back to the
  source. Keep both with the corpus, never in it if you copy the corpus
  elsewhere, and pass `--no-id-map` if you do not need traceability.
- **Retention:** delete the corpus and run directories when the evaluation is
  over, or keep them under the same retention policy as call recordings.
- **No external APIs:** the benchmark talks only to HopSTT endpoints. Existing
  Cartesia transcripts are imported from export files as a *baseline*. They
  are never sent anywhere and are never used as ground truth.

## 2. Manifest format (`manifest.jsonl`, one JSON object per line)

| key | required | type | meaning |
|---|---|---|---|
| `id` | yes | string `[A-Za-z0-9_-]{4,64}` | opaque id, no call ids or phone numbers |
| `audio` | yes | string | path relative to the corpus (WAV preferred; any format ffmpeg decodes) |
| `language` | yes | `en` \| `es` | language of the reference |
| `channel` | no | int ≥ 0 | channel to use from a multi-channel file (the leg) |
| `start`, `end` | no | seconds | segment of `audio` to use |
| `speech_start`, `speech_end` | no | seconds (relative to the segment) | speech onset/offset, used for time-to-first-partial and for `--finalize-at speech_end` (simulated VAD stop) |
| `reference` | yes | string | manually verified text. Empty string for silence or noise-only items |
| `reference_status` | yes | `verified` \| `unverified` | **only `verified` items count toward accuracy** |
| `tags` | no | list | from the vocabulary below |
| `critical_fields` | no | list of `{type, value, span?, polarity?}` | slots scored separately (see below) |
| `baseline` | no | `{engine: text}` | existing transcripts for the same audio, e.g. `{"cartesia": "..."}` |
| `duration`, `reviewed_by`, `reviewed_at`, `notes`, `x_*` | no | | informational |

**Tags**: `accent_<name>` (e.g. `accent_southern_us`, `accent_caribbean`,
`accent_mexican`), `older_caller`, `fast`, `slow`, `quiet`, `noisy`,
`distorted`, `low_volume`, `clipping`, `interruption`, `overlap`, `crosstalk`,
`voicemail`, `ivr`, `hold_music`, `silence`, `noise_only`, `short_yes`,
`short_no`, `names`, `spelling`, `numbers`, `negation`, `code_switch`,
`caller`, `agent`, `long`, `speakerphone`, `background_tv`. Add new ones in
`dataset.py:TAG_VOCABULARY`, not ad hoc.

**Critical field types**: `first_name`, `last_name`, `state`, `age`, `dob`,
`phone`, `coverage_amount`, `premium_amount`, `medication`, `condition`,
`yes_no`, `correction`, `callback_request`, `agent_transfer_consent`,
`do_not_contact`.

- `value` is the fact as the caller meant it, in any written form. It goes
  through the same normalizer, so `"TX"`, `"3/3/1951"` and `"$25,000"` are fine.
- `span` (optional) is `{start, end}`, character offsets into `reference`. With
  a span, the field is correct only if the hypothesis words aligned to that
  part of the reference contain the value. Without a span, the value may appear
  anywhere in the hypothesis.
- For `yes_no`, `do_not_contact`, `agent_transfer_consent` and `correction`
  the expected **polarity** (negative or affirmative) is taken from `polarity`
  if given, otherwise from `value` (e.g. `"no"`), otherwise from the reference
  text. The field is scored by polarity, not by exact words.

Example:

```json
{"id":"hs_kpgamhlbnoicejfd","audio":"audio/hs_kpgamhlbnoicejfd.wav","language":"en",
 "speech_start":0.35,"speech_end":2.9,
 "reference":"No, I don't smoke. I was born March 3rd, 1951.","reference_status":"verified",
 "tags":["older_caller","short_no","caller"],
 "critical_fields":[{"type":"yes_no","value":"no","span":{"start":0,"end":17}},
                    {"type":"dob","value":"3/3/1951"}],
 "baseline":{"cartesia":"No, I don't smoke. I was born March 3rd 1951."}}
```

## 3. Preparation

Input is a CSV, JSON or JSONL export with one row per recording, or per
recording leg or segment:

| column | meaning |
|---|---|
| `recording` | path to the recording (absolute, or relative to `--recordings-root`) |
| `call_id` | used only to derive the opaque id and in the private id map |
| `language` | `en` / `es` |
| `cartesia_transcript` | existing Cartesia text for exactly this row's audio (leg/segment) |
| `channel`, `start`, `end` | optional: the leg and segment to cut |
| `tags` | optional, `;`-separated |
| `cartesia_segments` | optional JSON list `[{start, end, text, channel?}]` (Hoppwhistle stores timed segments) |

```bash
python -m bench.dataset prepare --input /secure/exports/calls.csv \
    --recordings-root /secure/recordings --out /secure/hopstt-corpus \
    --split-by-baseline-segments --sample 300 --en-share 0.7
```

`prepare` does the following:

- expands each row into segments: the given `start`/`end`, or one item per
  Cartesia segment (with `--pad` seconds of padding) when you pass
  `--split-by-baseline-segments`, or else the whole file;
- splits stereo calls per channel (each leg becomes its own item), cuts the
  segment and writes **mono 8 kHz PCM16** WAVs as `audio/<id>.wav` (ffmpeg is
  needed for non-WAV input or resampling);
- writes `manifest.jsonl` with every item `unverified`. Its `reference` is the
  Cartesia text as a *starting draft*, and `baseline.cartesia` holds the same
  text, which stays untouched. A whole-call transcript is not attached to a
  single leg unless the export says which leg it belongs to;
- writes `review_sheet.csv` and `id_map.private.jsonl` (both `0600`);
- with `--sample N`, takes a stratified sample: about `en_share` English, the
  rest Spanish, round-robin across each row's first tag.

### Sampling plan (target ≥ 200 verified, ~70 % EN / 30 % ES)

Prepare about 300 segments, because review rejects some (wrong language,
unintelligible, consent issues), and aim for at least 210 EN and 90 ES before
review. Spread them across the tags. The table below gives minimums; for any
type of item not listed, take at least 10:

| slice | minimum verified |
|---|---|
| Spanish items | 50 (the gate needs ≥ 50; target ~30 %) |
| silence / noise-only / hold-music clips (empty reference) | 10 |
| negative answers in polarity fields (`short_no`, "not interested", do-not-call) | 20 |
| each of `first_name`, `last_name`, `state`, `age`, `dob`, `phone`, `yes_no` | 10 |
| `older_caller`, `noisy`/`distorted`/`low_volume`, `fast`, `overlap`/`interruption`, accents | 10 each where available |
| both legs (`caller` and `agent`) | caller ≥ 70 % (accuracy matters most there) |

Draw from several campaigns, days and times, not from one batch of calls.
`validate` prints the shortfalls against these targets.

### Reviewer procedure

1. Open `review_sheet.csv` on the secure host. Play `audio` for each row.
2. Correct `reference` so it matches **exactly what was said**: verbatim words,
   including repetitions and false starts that carry words. Leave out
   hesitation sounds (uh, um). Write numbers in whatever form you like, because
   the normalizer unifies them. Do not "fix" grammar. Do not use placeholders
   such as `[unintelligible]`: if a stretch cannot be understood, reject the row.
   **The Cartesia draft is only a starting point. Never accept it without
   listening.**
3. Fill in `critical_fields_json`, e.g.
   `[{"type":"yes_no","value":"no"},{"type":"age","value":"72"}]`, and adjust
   `tags`.
4. Set `reference_status` to `verified` and fill in `reviewer` (initials) and
   `reviewed_at`, or set it to `rejected` to drop the row.
5. Merge the sheet and validate:

   ```bash
   python -m bench.dataset apply-review --corpus /secure/hopstt-corpus \
       --review /secure/hopstt-corpus/review_sheet.csv
   python -m bench.dataset validate --corpus /secure/hopstt-corpus --check-audio
   ```

   Rows marked `verified` without a reviewer stay unverified. Have a second
   reviewer spot-check at least 10 % of the rows, and every Spanish row if only
   one reviewer is fluent.

`validate` exits 1 on schema errors. With `--strict` it exits 2 when the
distribution targets are not met.
