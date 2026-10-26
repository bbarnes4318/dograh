# Accent reference audio

One approved recording per accent, used once by
`scripts/provision_accent_clones.py` to create that accent's private Fish
instant-clone voice. The app never reads these files at runtime.

## Naming

```
001_reference.wav
021_reference.mp3
075_reference.m4a
```

`NNN` is the zero-padded `accent_id` from `../accents.json` (the dataset is
authoritative; a file's original name says nothing about its accent). Keep the
source format — Fish accepts WAV, MP3, M4A, FLAC, OGG/Opus — so the original is
uploaded unchanged. Exactly one `NNN_reference.*` file per accent: two would be
ambiguous and the accent is rejected.

If a file ever has to be converted, keep the original under `originals/`
(not scanned) and put the converted clone input here as `NNN_reference.wav`.

## Importing from arbitrary filenames

Map each file to its accent explicitly with a CSV (`accent_id,source_path`)
and let the script copy it into place under the normalized name:

```
python scripts/provision_accent_clones.py --data-dir api/data/accent_voices \
  --import-audio /path/to/mapping.csv --dry-run
```

## Privacy

These recordings are git-ignored and excluded from the API Docker image.
Previews in `../previews/` are synthesized from the Fish clone, not copied
from here.
