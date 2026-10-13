"""
Retry flush captures that an earlier FAILED flush left on disk (WI-bugfix-1b9f04).

Since WI-bugfix-4d7e21 a failed flush keeps its context file instead of deleting it. But
every producer (session-end.py, pre-compact.py, backfill.py) mints a NEW file and flush.py
only reads the path on its argv, so nothing ever read a kept file back. This is that reader.

Run nightly by run_flush.bat, after the day's backfill:
    uv run python scripts/retry_preserved.py

Rules (owner decisions 2026-10-13):
  - oldest first, at most MAX_PER_RUN per run;
  - STOP at the first failure - a usage limit then costs one attempt, not ten;
  - after MAX_ATTEMPTS failed runs, MOVE the file to preserved-failed/ - never delete it.
Files younger than MIN_AGE_S are skipped: a live flush has its context file on disk while it
runs, and retrying that would save the same session twice.

Known limit: if a session+date is backfilled successfully AFTER an earlier capture of it was
preserved, the retry adds a second summary for that day. The nightly task only backfills
today, so that takes a manual re-backfill.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import backfill  # noqa: E402

SCRIPTS_DIR = backfill.SCRIPTS_DIR
ATTEMPTS_FILE = SCRIPTS_DIR / "preserved-retries.json"
FAILED_DIR = SCRIPTS_DIR / "preserved-failed"
MAX_PER_RUN = 10
MAX_ATTEMPTS = 3
MIN_AGE_S = 3600

# backfill-<sid>-<YYYY-MM-DD>-<YYYYMMDD-HHMMSS>.md  (backfill.py)
# session-flush-<sid>-<YYYYMMDD-HHMMSS>.md          (session-end.py)
# flush-context-<sid>-<YYYYMMDD-HHMMSS>.md          (pre-compact.py)
_NAME = re.compile(
    r"^(?:backfill|session-flush|flush-context)-(?P<sid>.+?)-"
    r"(?:(?P<date>\d{4}-\d{2}-\d{2})-)?(?P<stamp>\d{8})-\d{6}\.md$"
)


def parse_name(name: str) -> tuple[str, str] | None:
    """(session_id, original YYYY-MM-DD) from a capture's file name, or None."""
    m = _NAME.match(name)
    if not m:
        return None
    if m["date"]:
        return m["sid"], m["date"]
    s = m["stamp"]
    return m["sid"], f"{s[:4]}-{s[4:6]}-{s[6:]}"


def preserved_captures(now: float) -> list[Path]:
    """Kept captures old enough to retry, oldest first."""
    files = [
        p for pattern in ("backfill-*.md", "session-flush-*.md", "flush-context-*.md")
        for p in SCRIPTS_DIR.glob(pattern)
        if now - p.stat().st_mtime >= MIN_AGE_S
    ]
    return sorted(files, key=lambda p: p.stat().st_mtime)


def _load_attempts() -> dict:
    try:
        return json.loads(ATTEMPTS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def main() -> int:
    attempts = _load_attempts()
    saved = failed = set_aside = 0
    tried = 0
    for capture in preserved_captures(time.time()):
        if tried >= MAX_PER_RUN:
            break
        parsed = parse_name(capture.name)
        if parsed is None:
            print(f"  SKIP {capture.name}: name not recognised, left alone", flush=True)
            continue
        session_id, target_date = parsed
        tried += 1
        print(f"  RETRY {capture.name}", flush=True)
        if backfill.invoke_flush(capture, session_id, target_date):
            saved += 1
            attempts.pop(capture.name, None)
            continue
        failed += 1
        attempts[capture.name] = attempts.get(capture.name, 0) + 1
        if attempts[capture.name] >= MAX_ATTEMPTS and capture.exists():
            FAILED_DIR.mkdir(exist_ok=True)
            shutil.move(str(capture), str(FAILED_DIR / capture.name))
            attempts.pop(capture.name)
            set_aside += 1
            print(f"  SET ASIDE {capture.name}: failed {MAX_ATTEMPTS} runs", flush=True)
        break  # stop at the first failure (see module docstring)

    # Drop counts for files that are gone (saved by hand, or deleted) so the file cannot grow.
    attempts = {k: v for k, v in attempts.items() if (SCRIPTS_DIR / k).exists()}
    ATTEMPTS_FILE.write_text(json.dumps(attempts, indent=1), encoding="utf-8")
    waiting = len(preserved_captures(float("inf")))
    print(
        f"retry-preserved: {saved} saved, {failed} failed, {set_aside} set aside, "
        f"{waiting} still waiting", flush=True,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
