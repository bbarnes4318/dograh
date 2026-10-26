# Local accent voices

One private Fish **instant clone** per US regional accent, chosen per call
from the prospect's area code when a Fish TTS config uses the voice
`auto:local-accent` ("Auto — Local Accent by Area Code").

```
423 ─┐
729 ─┼── accent_id 21 ── one Fish reference_id
865 ─┘
```

| File | What | Used at runtime |
| --- | --- | --- |
| `accents.json` | Authoritative 75-accent dataset (id, name, states, area codes, preview text) | names |
| `area_code_accent_map.json` | Every area code → its single `accent_id`; default accent 68 | yes |
| `accent_clone_registry.json` | accent → `fish_voice_id`, title, status (written by the provisioning script) | yes |
| `accent_routing_map.json` | area code → accent → `fish_voice_id` (derived, for audit) | no |
| `clone_manifest.json` | accent → approved reference audio (derived) | no |
| `clone_provision_log.csv` | one row per clone attempt | no |
| `previews/NNN.mp3` | TTS sample synthesized from each clone, served by `GET /api/v1/public/accent-voices/previews/NNN.mp3` | yes |
| `reference_audio/` | approved cloning inputs — git-ignored, not in the Docker image, never served | no |

Only entries with `"status": "ready"` are routed to. Unprovisioned accents, and
toll-free / non-US / malformed / missing numbers, use accent 68; if 68 has no
clone either, calls use the standard Fish voice. Nothing is cloned during a
call.

## Provisioning

```bash
pip install 'fish-audio-sdk==1.3.0'
export FISH_API_KEY='...'          # never commit or paste it anywhere
python scripts/provision_accent_clones.py --data-dir api/data/accent_voices
```

Re-running is safe: accents that already have a `fish_voice_id` are skipped.
`--only 21` limits the run; `--force 21` re-clones (the old id is kept in
`previous_fish_voice_ids`, nothing is deleted on Fish). See the script's
docstring for everything else.
