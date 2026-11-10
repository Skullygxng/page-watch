#!/usr/bin/env python3
"""watch.py — fetch watched pages, diff against the last snapshot,
and write a log entry ONLY when something actually changed.

Quiet by design: silence means nothing happened. A line in data/changes.json
means something did. A run with no changes touches no files at all, so the
workflow's commit step has nothing to commit and Pages never rebuilds.
"""
import difflib
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.json"
DATA = ROOT / "data"
SNAPSHOTS = DATA / "snapshots"
ARCHIVE = SNAPSHOTS / "archive"
CHANGES = DATA / "changes.json"

MAX_DIFF_LINES = 100  # ~several distinct changes; counts (+N/-N) still show if trimmed
MAX_LINE_LEN = 200

# Best-effort: covers "Last updated: 14 March 2025" and the PBC-style
# "DATE OF ISSUE: May 30, 2025" that the roads page uses.
STAMP_RE = re.compile(
    r"(last\s+updated|page\s+last\s+updated|last\s+modified|date\s+published|date\s+of\s+issue)"
    r"\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9 ,:/\-]*)",
    re.IGNORECASE,
)


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize(text):
    """Collapse whitespace so formatting churn never counts as a change."""
    lines = (re.sub(r"\s+", " ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def extract(page, html):
    """Pull the main content, excluding sidebar/nav noise, with fallbacks."""
    soup = BeautifulSoup(html, "html.parser")
    for sel in page.get("exclude_selectors", []):
        for el in soup.select(sel):
            el.decompose()

    root = None
    # page's own selector (if set), then main, then #content, then body
    for sel in [page.get("selector"), "main", "#content", "body"]:
        if not sel:
            continue
        candidate = soup.select_one(sel)
        if candidate is not None and candidate.get_text(strip=True):
            root = candidate
            break
    if root is None:
        raise ValueError("no selector in the fallback chain matched any content")
    return root.get_text("\n", strip=True)


def fetch(url):
    headers = {
        "User-Agent": "council-watcher/1.0 (personal page-change monitor; one fetch per day)"
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.text


def stamp_of(text):
    match = STAMP_RE.search(text)
    return match.group(0).strip() if match else ""


def diff_summary(before, after):
    lines = list(
        difflib.unified_diff(
            before.splitlines(), after.splitlines(), fromfile="before", tofile="after", lineterm=""
        )
    )
    added = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
    trimmed = [
        ln[:MAX_LINE_LEN] for ln in lines if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))
    ][:MAX_DIFF_LINES]
    return added, removed, trimmed


def save_snapshot(name, text, digest, stamp):
    (SNAPSHOTS / f"{name}.txt").write_text(text, encoding="utf-8")
    meta = {"hash": digest, "stamp": stamp, "captured": utc_now()}
    (SNAPSHOTS / f"{name}.meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def archive_old(name, keep):
    old = SNAPSHOTS / f"{name}.txt"
    if not old.exists():
        return
    target = ARCHIVE / f"{name}-{utc_now().replace(':', '-')}.txt"
    old.replace(target)
    snapshots = sorted(ARCHIVE.glob(f"{name}-*.txt"))
    for excess in snapshots[:-keep] if keep > 0 else snapshots:
        excess.unlink(missing_ok=True)


def load_entries():
    if CHANGES.exists():
        return json.loads(CHANGES.read_text(encoding="utf-8"))
    return []


def watch_page(page, entries, config):
    name = page["name"]
    url = page["url"]
    html = fetch(url)
    text = normalize(extract(page, html))
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    stamp = stamp_of(text)

    meta_path = SNAPSHOTS / f"{name}.meta.json"
    if not meta_path.exists():
        save_snapshot(name, text, digest, stamp)
        entries.insert(0, {
            "timestamp": utc_now(),
            "type": "baseline",
            "name": name,
            "url": url,
            "hash": digest,
            "stamp": stamp,
            "note": "first snapshot taken; future runs diff against this",
        })
        print(f"[{name}] baseline snapshot saved ({len(text.splitlines())} lines)")
        return

    prev_meta = json.loads(meta_path.read_text(encoding="utf-8"))
    prev_text_path = SNAPSHOTS / f"{name}.txt"
    prev_text = prev_text_path.read_text(encoding="utf-8") if prev_text_path.exists() else ""

    hash_changed = prev_meta.get("hash") != digest
    stamp_changed = bool(stamp) and prev_meta.get("stamp") != stamp

    if not (hash_changed or stamp_changed):
        print(f"[{name}] quiet — nothing changed")
        return

    added, removed, diff_lines = diff_summary(prev_text, text)
    archive_old(name, config.get("keep_snapshots", 30))
    save_snapshot(name, text, digest, stamp)
    entries.insert(0, {
        "timestamp": utc_now(),
        "type": "change",
        "name": name,
        "url": url,
        "hash_changed": hash_changed,
        "stamp_changed": stamp_changed,
        "stamp_before": prev_meta.get("stamp", ""),
        "stamp_after": stamp,
        "lines_added": added,
        "lines_removed": removed,
        "diff": diff_lines,
    })
    print(f"[{name}] CHANGE logged: +{added} / -{removed} lines"
          + (" (stamp changed)" if stamp_changed and not hash_changed else ""))


def main():
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    pages = [p for p in config.get("pages", []) if p.get("enabled")]
    if not pages:
        print("no enabled pages in config.json")
        return 0

    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    entries = load_entries()
    before_count = len(entries)
    failures = 0

    for page in pages:
        try:
            watch_page(page, entries, config)
        except Exception as exc:
            failures += 1
            print(f"[{page['name']}] ERROR: {exc}")

    # Only rewrite changes.json if something was logged, so a quiet run
    # leaves the working tree untouched and the workflow commits nothing.
    if len(entries) != before_count:
        cap = config.get("max_log_entries", 200)
        CHANGES.write_text(json.dumps(entries[:cap], indent=2), encoding="utf-8")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
