# Manual export (Phase 2 Immich check)

Before any ML runs, confirm that Immich picks up what the exporter writes.

1. Put 3 scans next to a `batch.json`:

```json
{
  "batch": "immich-smoke-test",
  "box_label": "Attic Box 3",
  "items": [
    {"front": "Box3_0001.jpg", "back": "Box3_0001_b.jpg", "date": "Xmas '84",
     "description": "Christmas morning at Grandma's", "people": ["Grandma", "John"], "events": ["Christmas"]},
    {"front": "Box3_0002.jpg", "date": "Summer of '79", "places": ["Lake Erie"]},
    {"front": "Box3_0003.jpg", "date": {"precision": "decade", "year": 1960}}
  ]
}
```

`date` is either free text (parsed with the same rules used for photo backs) or an explicit
`{precision, year, month, day, season, circa}` object.

2. Point `paths.sorted` at a test library folder and run:

```
banana export --manual-json path/to/batch.json
```

3. In Immich (Administration → External Libraries), add the folder's **container** path as a
library, add the exclusion pattern `**/.staging/**`, and scan it. Check:

- Timeline placement: 1984-12-25, 1979-07-15 (summer), 1965-07-01 (1960s).
- Description shows the text, with `(date approx: …)` for non-exact dates.
- Tags appear as a hierarchy: `People/John`, `Events/Christmas`, `Box/Attic Box 3`, `Scan/HasBack`.
- The back `SCAN_000001_B.jpg` shows up as a separate asset (it's stacked automatically in Phase 5).

Rerunning the command is safe: the same scan keeps its id and overwrites, or moves if its date changed.
