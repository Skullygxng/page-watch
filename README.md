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
