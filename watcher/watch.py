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
import time
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


RAW = DATA / "raw"
CONFIRM_DELAY_SECONDS = 90


def rounding_unit(value):
    """The unit a display-rounded counter quantizes to, inferred from the
    reading itself (its trailing zeros). 77,600,000 -> 100000; 41,208 -> 1.
    Exact counters therefore can never sit in a boundary zone.
    """
    if value == 0:
        return 1
    unit = 1
    v = value
    while v % 10 == 0:
        unit *= 10
        v //= 10
    return unit


def append_raw(name, record):
    """The raw history: one line per fetch, appended, never trimmed, no
    threshold applied. changes.json is a view over this, not the other
    way around. If you want the willsmith jitter in a month, it is here.
    """
    RAW.mkdir(parents=True, exist_ok=True)
    with open(RAW / f"{name}.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def read_raw_tail(name, n=6):
    path = RAW / f"{name}.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").strip().splitlines()[-n:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def fetch_number(page, name):
    """One fetch -> one extracted integer, with the fallback chain. The
    caller decides how many times to call this. Also returns the soup so
    the engagement pass can read the same payload without a second fetch.
    """
    html = fetch(page["url"], page.get("user_agent"))
    soup = BeautifulSoup(html, "html.parser")
    for sel in page.get("exclude_selectors", []):
        for el in soup.select(sel):
            el.decompose()
    element = soup.select_one(page["extract_number"])
    if element is not None:
        try:
            return parse_number(element.get_text(" ", strip=True)), "markup", soup
        except ValueError:
            pass
    value = number_from_hydration(soup, page["extract_number"])
    print(f"[{name}] markup selector missed — used hydration JSON fallback")
    return value, "hydration", soup


def hydration_stats(soup):
    """Engagement stats from the same hydration blob that carries the
    follower count: followers, total hearts (likes across all posts) and
    post count — all exact integers. Per-video views are NOT here: TikTok
    renders the video grid client-side (SSR itemList is empty even when
    videoCount > 0), so videos_in_payload is recorded in raw for
    diagnostics only. Raises when nothing is on any known path — the same
    fail-loud rule: a missing engagement number is not zero.
    """
    script = soup.find("script", id="__UNIVERSAL_DATA_FOR_REHYDRATION__")
    if script is None:
        raise ValueError("no hydration JSON found — engagement stats unavailable")
    try:
        data = json.loads(script.string or script.get_text())
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"hydration JSON failed to parse: {exc}") from exc

    detail = (data.get("__DEFAULT_SCOPE__") or {}).get("webapp.user-detail") or {}
    user_info = detail.get("userInfo") or {}
    stats = user_info.get("stats") or {}
    stats_v2 = user_info.get("statsV2") or {}
    shared = _deep_find(data, "user_state") or {}
    shared_stats = (shared.get("userInfo") or {}).get("stats") or {}

    def pick(int_key, str_key):
        for src in (stats, shared_stats):
            v = src.get(int_key)
            if isinstance(v, int):
                return v
        v = stats_v2.get(str_key)
        if v:
            return parse_number(str(v))
        return None

    followers = pick("followerCount", "followerCount")
    hearts = pick("heartCount", "heartCount")
    videos = pick("videoCount", "videoCount")
    if followers is None and hearts is None and videos is None:
        raise ValueError(
            "hydration JSON had no engagement stats on any known path — failing, not logging zeros"
        )
    item_list = user_info.get("itemList")
    return {
        "followers": followers,
        "hearts": hearts,
        "videos": videos,
        "videos_in_payload": len(item_list) if isinstance(item_list, list) else None,
    }


def detect_zone(name, unit):
    """A page is IN the boundary zone when its recent raw readings include
    a pair exactly one rounding unit apart — the fingerprint of a true
    value sitting on a display-rounding edge, where every flip is phantom.
    """
    if unit < 100:  # exact or fine-grained counters cannot jitter by a unit
        return False
    values = [r.get("value") for r in read_raw_tail(name)
              if isinstance(r.get("value"), int)]
    return any(abs(b - a) == unit for a, b in zip(values, values[1:]))


def watch_number_page(page, entries, config):
    """number_mode: extract one integer, record it raw, log the delta.

    Policies, all written down:
    - Selector rot: nothing matches or nothing parses -> raise. A missing
      number is not zero; the page fails loudly.
    - min_delta_pct (default 0.5, per-page overridable): a move must exceed
      prev_value * pct / 100 to log. It filters the VIEW, never the record.
    - Above threshold: re-read CONFIRM_DELAY_SECONDS later, inside the same
      run. THE SECOND READING DECIDES: it becomes `current` and the
      snapshot; the first reading is kept in the entry (`first_reading`)
      and in raw. If the second reading reverts below threshold, nothing
      is logged and both readings live only in raw history.
    - Boundary zone: recent readings differing by exactly one rounding unit
      flag the page (entry type "boundary" on enter/leave, warning on the
      dashboard) so phantom one-unit flips are identifiable up front.
    """
    name = page["name"]
    url = page["url"]
    min_pct = page.get("min_delta_pct", 0.5)

    # Reading 1 — recorded raw no matter what happens next. On engagement
    # pages the same payload also yields the stats block; if it doesn't,
    # the reading is still recorded raw, then the page fails loudly.
    value, source, soup = fetch_number(page, name)
    raw_record = {"timestamp": utc_now(), "value": value, "source": source, "reading": 1}
    engage = None
    if page.get("engagement"):
        try:
            engage = hydration_stats(soup)
            raw_record["stats"] = engage
        except ValueError as exc:
            raw_record["stats_error"] = str(exc)
            append_raw(name, raw_record)
            raise
    append_raw(name, raw_record)

    meta_path = SNAPSHOTS / f"{name}.meta.json"
    meta = {"value": value, "in_zone": False, "captured": utc_now()}
    if not meta_path.exists():
        if page.get("simulate"):
            meta["simulated"] = True
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        entry = {
            "timestamp": utc_now(),
            "type": "baseline",
            "name": name,
            "url": url,
            "value": value,
            "note": "first number captured; future runs log the daily delta",
        }
        if page.get("simulate"):
            entry["simulated"] = True
        entries.insert(0, entry)
        print(f"[{name}] baseline number captured: {value:,}")
        return

    prev = json.loads(meta_path.read_text(encoding="utf-8"))
    prev_value = prev.get("value", 0)
    threshold = abs(prev_value) * min_pct / 100.0
    delta = value - prev_value
    logged_entry = None

    if abs(delta) > threshold:
        # Above threshold: the 90-second confirmation re-read, same run.
        print(f"[{name}] move {delta:+,} exceeds threshold {threshold:,.0f} "
              f"({min_pct}% of {prev_value:,}) — re-reading in {CONFIRM_DELAY_SECONDS}s")
        time.sleep(CONFIRM_DELAY_SECONDS)
        value2, source2, _soup2 = fetch_number(page, name)
        append_raw(name, {"timestamp": utc_now(), "value": value2, "source": source2, "reading": 2})

        # THE SECOND READING DECIDES.
        if abs(value2 - prev_value) > threshold:
            delta = value2 - prev_value
            meta = {"value": value2, "in_zone": prev.get("in_zone", False), "captured": utc_now()}
            logged_entry = {
                "timestamp": utc_now(),
                "type": "number",
                "name": name,
                "url": url,
                "previous": prev_value,
                "current": value2,
                "delta": delta,
                "first_reading": value,
                "confirmed": value2 == value,
                "threshold_pct": min_pct,
            }
            print(f"[{name}] CONFIRMED: first read {value:,}, second read {value2:,} "
                  f"vs {prev_value:,} — logged ({delta:+,})")
        else:
            print(f"[{name}] first read {value:,}, second read {value2:,} — move NOT "
                  "confirmed; logged as quiet, both readings kept in raw history")
            value = value2  # for zone detection only; snapshot unchanged
    else:
        print(f"[{name}] quiet — {value:,} (within threshold {threshold:,.0f})")

    # Rounding-boundary zone: entering or leaving is always logged.
    unit = rounding_unit(value)
    in_zone = detect_zone(name, unit)
    zone_changed = in_zone != prev.get("in_zone", False)

    if logged_entry is not None:
        if page.get("simulate"):
            logged_entry["simulated"] = True
            print(f"[{name}] SIMULATED delta — test flag on in config; excluded from dashboard math")
        entries.insert(0, logged_entry)
    if zone_changed:
        # The snapshot only moves when a move is actually logged. On a
        # quiet run (even with a zone transition), the baseline stays at
        # the last LOGGED value — sub-threshold drift must accumulate in
        # raw history until it crosses the threshold, not leak into meta.
        meta = {
            "value": logged_entry["current"] if logged_entry else prev_value,
            "in_zone": in_zone,
            "captured": utc_now(),
        }
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        entries.insert(0, {
            "timestamp": utc_now(),
            "type": "boundary",
            "name": name,
            "url": url,
            "in_zone": in_zone,
            "unit": unit,
            "value": value,
        })
        print(f"[{name}] {'ENTERED' if in_zone else 'LEFT'} rounding-boundary zone "
              f"(unit {unit:,}) — read its column with caution")
    if logged_entry is not None:
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    # Engagement pair: followers vs total hearts vs post count, from the
    # same payload. Logged whenever hearts or posts move (follower moves
    # have their own entry); a missing stats block already failed above,
    # so this branch only ever sees real numbers.
    if engage is not None:
        hearts = engage.get("hearts")
        videos = engage.get("videos")
        engage_path = SNAPSHOTS / f"{name}.engage.json"
        prev_engage = None
        if engage_path.exists():
            prev_engage = json.loads(engage_path.read_text(encoding="utf-8"))
        changed = (prev_engage is None
                   or hearts != prev_engage.get("hearts")
                   or videos != prev_engage.get("videos"))
        if changed:
            entry = {
                "timestamp": utc_now(),
                "type": "engagement",
                "name": name,
                "url": url,
                "followers": value,
                "hearts": hearts,
                "videos": videos,
            }
            if prev_engage is None:
                entry["note"] = "engagement baseline"
            else:
                entry["hearts_delta"] = (hearts or 0) - (prev_engage.get("hearts") or 0)
                entry["videos_delta"] = (videos or 0) - (prev_engage.get("videos") or 0)
            if page.get("simulate"):
                entry["simulated"] = True
            entries.insert(0, entry)
            engage_path.write_text(json.dumps(
                {"hearts": hearts, "videos": videos, "captured": utc_now()},
                indent=2,
            ), encoding="utf-8")
            print(f"[{name}] engagement logged: {value:,} followers, "
                  f"{hearts:,} hearts, {videos:,} posts")


def main():
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    pages = [p for p in config.get("pages", []) if p.get("enabled")]
    if not pages:
        print("no enabled pages in config.json")
        return 0

    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    RAW.mkdir(parents=True, exist_ok=True)
    entries = load_entries()
    before_count = len(entries)
    failures = 0

    for index, page in enumerate(pages):
        if index:
            time.sleep(2)  # small steady gap between pages — polite to the host, and not a burst
        try:
            if page.get("number_mode"):
                watch_number_page(page, entries, config)
            else:
                watch_page(page, entries, config)
        except Exception as exc:
            failures += 1
            print(f"[{page['name']}] ERROR: {exc}")

    # changes.json is rewritten only when something was logged, so a fully
    # quiet run leaves the DASHBOARD VIEW untouched. Raw history (data/raw/)
    # is appended every run by design, so the commit-back still happens —
    # that is the record, not the view.
    if len(entries) != before_count:
        cap = config.get("max_log_entries", 200)
        CHANGES.write_text(json.dumps(entries[:cap], indent=2), encoding="utf-8")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
# trigger: manual verification run 2026-11-10
