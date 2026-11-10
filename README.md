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
per-page table of the last 7 days with the average rate of change.

If the markup selector misses — some sites serve more than one page
variant — the extractor falls back to the page's server-injected JSON
hydration blob (`script#__UNIVERSAL_DATA_FOR_REHYDRATION__`), which
usually carries the exact integer even when the visible text is
K/M-abbreviated. If both paths fail, the page still fails. An optional
per-page `user_agent` overrides the default fetcher UA, which some sites
need before they serve their server-rendered markup at all.

## Files

| Path | Purpose |
| --- | --- |
| `config.json` | Pages to watch: URL, optional CSS selector, exclude list |
| `watcher/watch.py` | Fetch, extract, diff, log |
| `requirements.txt` | `requests`, `beautifulsoup4` |
| `.github/workflows/watch.yml` | Daily cron + manual trigger |
| `index.html` | Dashboard |
| `data/changes.json` | Change log (written by the workflow) |
| `data/snapshots/` | Latest + archived snapshots |

## Notes

- The commit step only ever commits `data/`, and a quiet run leaves the
  tree untouched, so quiet days produce no commits and no rebuilds.
- The workflow has a `paths:` filter and a `concurrency` group so its own
  commit-back never re-triggers it and runs never overlap.
