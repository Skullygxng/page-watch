# page-watch

Personal page monitoring. A GitHub Actions workflow checks a local council
web page every morning, compares it against the previous snapshot, and
records what changed to a static dashboard served by GitHub Pages.

## How it works

- `watcher/watch.py` fetches each enabled page in `config.json`, strips
  sidebar/nav noise, normalizes whitespace, and hashes the extracted text.
- It also reads the page's "last updated" stamp, if present.
- If neither the hash nor the stamp changed since the last snapshot,
  **nothing is written and nothing is committed** — silence means nothing
  happened.
- If something changed, the old snapshot is archived and an entry is
  prepended to `data/changes.json` (with a trimmed diff). The workflow
  commits `data/` back, which triggers a Pages rebuild.
- `index.html` reads `data/changes.json` and renders the log. Add it to
  the Home Screen on a phone to use it like an app.

### number_mode

Set `"number_mode": true` plus `"extract_number": "<css selector>"` on a
page to watch a single integer (a follower count, say) instead of diffing
text. The element's text is parsed as an exact integer — `41,208` →
`41208`, `41.2K` → `41200` — and one log entry per day records the delta.
Unchanged numbers stay quiet, like everything else.

If the selector matches nothing or the text has no parsable number, the
page **fails loudly and logs nothing**: a missing number is not zero, and
a fake drop is worse than silence. The dashboard prints stored values
with thousands separators and never re-formats to K/M, and shows a
per-page table of the last 7 days with the average rate of change. The
dashboard also renders a cross-account matrix: one row per day, one column
per watched number, and a bottom count of how many accounts dropped that
day — the row highlights when several accounts drop at once.

If the markup selector misses — some sites serve more than one page
variant — the extractor falls back to the page's server-injected JSON
hydration blob (`script#__UNIVERSAL_DATA_FOR_REHYDRATION__`), which
usually carries the exact integer even when the visible text is
K/M-abbreviated. If both paths fail, the page still fails. An optional
per-page `user_agent` overrides the default fetcher UA, which some sites
need before they serve their server-rendered markup at all.

#### min_delta_pct, raw history, boundary zone, confirmation

- `min_delta_pct` (default 0.5, per-page overridable): a move must exceed
  `previous × pct / 100` to be logged. The threshold scales with the
  account it protects — at 32 followers it rounds to nothing, so every
  move logs; at 77.6M it is ~388K, so a 100K display-rounding flip logs
  as quiet while a real purge still logs. It filters the *view*, never
  the record.
- **Raw history**: every fetch appends a line to `data/raw/<name>.jsonl`
  — timestamp, exact value, extraction source, reading number. Appended,
  never trimmed, no threshold applied. `changes.json` and the dashboard
  are views over that history, not the history itself.
- **Boundary zone**: the rounding unit is inferred from a reading's
  trailing zeros (77,600,000 → 100,000; 41,208 → 1). When recent raw
  readings differ by exactly one unit, the true value is sitting on a
  display-rounding edge and flips are phantom — the page is flagged with
  ⚠ on the dashboard, and an entry of type `"boundary"` is logged when
  it enters or leaves the zone.
- **Confirmation re-read**: a move above threshold triggers a second
  fetch 90 seconds later, inside the same run. THE SECOND READING
  DECIDES: it becomes `current` and the snapshot, and the first reading
  is preserved in the entry (`first_reading`, `confirmed`) and in raw
  history. If the second reading reverts below threshold, nothing is
  logged — both readings live only in raw history. Each fetch writes
  exactly one raw row, so a confirmed move produces two rows.

A page can carry `"simulate": true` during testing: any delta it logs
while the flag is set is written with `"simulated": true` on the record
itself, in `data/changes.json`. The dashboard keeps simulated entries
visible in their own section but excludes them from every rate and
matrix calculation, so the math only ever reflects real moves.

#### engagement: the pair

A page with `"engagement": true` also reads total hearts (likes across
all posts) and post count from the same hydration payload — exact
integers, no extra fetch. An entry of type `"engagement"` is logged
whenever hearts or posts move, carrying followers, hearts, posts and
their deltas; the raw history records the whole stats block every run.
Per-video views are not obtainable without JavaScript (TikTok renders
the profile video grid client-side; the SSR payload carries an empty
itemList even when videoCount > 0) — the raw row records
`videos_in_payload` for diagnostics. The dashboard's engagement-pair
table shows followers vs. hearts vs. posts and the hearts-per-follower
ratio: organic audiences hold that ratio roughly steady; a bought count
drives it toward zero. That ratio is the audit.

## Files

| Path | Purpose |
| --- | --- |
| `config.json` | Pages to watch: URL, optional CSS selector, exclude list |
| `watcher/watch.py` | Fetch, extract, diff, log |
| `requirements.txt` | `requests`, `beautifulsoup4` |
| `.github/workflows/watch.yml` | Daily cron + manual trigger |
| `index.html` | Dashboard |
| `data/changes.json` | Change log — the dashboard's view over raw history |
| `data/raw/` | Raw history: one line per fetch, never trimmed, no threshold |
| `data/snapshots/` | Latest + archived snapshots |

## Notes

- The commit step only ever commits `data/`, and a quiet run leaves the
  dashboard *view* untouched — but raw history is appended every run by
  design, so the repo always moves and the schedule never goes inactive.
- The workflow has a `paths:` filter and a `concurrency` group so its own
  commit-back never re-triggers it and runs never overlap.
