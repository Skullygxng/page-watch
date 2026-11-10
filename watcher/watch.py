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


DEFAULT_USER_AGENT = "council-watcher/1.0 (personal page-change monitor; one fetch per day)"


def fetch(url, user_agent=None):
    headers = {"User-Agent": user_agent or DEFAULT_USER_AGENT}
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


NUMBER_RE = re.compile(r"([0-9][\d.,]*)\s*([kmb])?", re.IGNORECASE)


def parse_number(text):
    """'41,208' -> 41208; '41.2K' -> 41200; '1.3M' -> 1300000.

    The stored value is an exact integer (int(round(n * multiplier)), so
    41.2K stores as 41200). The dashboard prints stored values with
    thousands separators and never re-formats them to K/M — what you read
    is what was stored.

    Raises ValueError when no number is present. A missing number is not
    zero: the page fails loudly and nothing is logged.
    """
    cleaned = text.replace("\u00a0", " ").strip()
    match = NUMBER_RE.search(cleaned)
    if not match:
        raise ValueError(f"no parsable number in {text[:60]!r}")
    digits = match.group(1).replace(",", "")
    suffix = (match.group(2) or "").lower()
    multiplier = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}.get(suffix, 1)
    return int(round(float(digits) * multiplier))


def _deep_find(obj, key):
    """First value found at any depth for a dict key, or None."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found = _deep_find(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _deep_find(v, key)
            if found is not None:
                return found
    return None


def _deep_find_action_count(obj, action_type):
    """First userInteractionCount on a schema.org InteractionCounter of the
    given @type (e.g. FollowAction), at any depth, or None."""
    if isinstance(obj, dict):
        if obj.get("@type") == action_type and isinstance(obj.get("userInteractionCount"), int):
            return obj["userInteractionCount"]
        for v in obj.values():
            found = _deep_find_action_count(v, action_type)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _deep_find_action_count(v, action_type)
            if found is not None:
                return found
    return None


def number_from_hydration(soup, selector):
    """Fallback when the markup selector misses.

    Single-page-app sites (TikTok included) inject a JSON hydration blob —
    script#__UNIVERSAL_DATA_FOR_REHYDRATION__ — that carries the exact
    integer even when the visible text is K/M-abbreviated or the markup
    variant changes between loads. If nothing usable is on any known path,
    this raises: still fail-don't-fake-zero.
    """
    script = soup.find("script", id="__UNIVERSAL_DATA_FOR_REHYDRATION__")
    if script is None:
        raise ValueError(
            f"selector {selector!r} matched nothing and no hydration JSON was found — failing, not logging 0"
        )
    try:
        data = json.loads(script.string or script.get_text())
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"hydration JSON failed to parse: {exc}") from exc

    detail = (data.get("__DEFAULT_SCOPE__") or {}).get("webapp.user-detail") or {}
    user_info = detail.get("userInfo") or {}
    stats = user_info.get("stats") or {}
    if isinstance(stats.get("followerCount"), int):
        return stats["followerCount"]
    stats_v2 = user_info.get("statsV2") or {}
    if stats_v2.get("followerCount"):
        return parse_number(str(stats_v2["followerCount"]))
    shared = _deep_find(data, "user_state") or {}
    shared_stats = (shared.get("userInfo") or {}).get("stats") or {}
    if isinstance(shared_stats.get("followerCount"), int):
        return shared_stats["followerCount"]
    count = _deep_find_action_count(data, "FollowAction")
    if count is not None:
        return count
    raise ValueError(
        "hydration JSON had no follower count on any known path — failing, not logging 0"
    )


def watch_number_page(page, entries, config):
    """number_mode: extract one integer from the page and log the delta.

    Selector rot policy: if extract_number matches nothing, or its text has
    no parsable number, this raises — the run shows a red X and the log
    records NOTHING, because a missing number is not zero and a fake drop
    is worse than silence.
    """
    name = page["name"]
    url = page["url"]
    html = fetch(url, page.get("user_agent"))
    soup = BeautifulSoup(html, "html.parser")
    for sel in page.get("exclude_selectors", []):
        for el in soup.select(sel):
            el.decompose()
    element = soup.select_one(page["extract_number"])
    value = None
    if element is not None:
        try:
            value = parse_number(element.get_text(" ", strip=True))
        except ValueError:
            element = None
    if value is None:
        value = number_from_hydration(soup, page["extract_number"])
        print(f"[{name}] markup selector missed — used hydration JSON fallback")

    meta_path = SNAPSHOTS / f"{name}.meta.json"
    meta = {"value": value, "captured": utc_now()}
    if not meta_path.exists():
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        entries.insert(0, {
            "timestamp": utc_now(),
            "type": "baseline",
            "name": name,
            "url": url,
            "value": value,
            "note": "first number captured; future runs log the daily delta",
        })
        print(f"[{name}] baseline number captured: {value:,}")
        return

    prev = json.loads(meta_path.read_text(encoding="utf-8"))
    if prev.get("value") == value:
        print(f"[{name}] quiet — {value:,}, no change")
        return

    delta = value - prev["value"]
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    entries.insert(0, {
        "timestamp": utc_now(),
        "type": "number",
        "name": name,
        "url": url,
        "previous": prev["value"],
        "current": value,
        "delta": delta,
    })
    print(f"[{name}] number moved {prev['value']:,} -> {value:,} ({delta:+,})")


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
            if page.get("number_mode"):
                watch_number_page(page, entries, config)
            else:
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
