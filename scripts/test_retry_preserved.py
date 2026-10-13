"""Tests for WI-bugfix-1b9f04: a capture a failed flush kept on disk is retried automatically.

Run:  uv run --directory <project> python scripts/test_retry_preserved.py

No LLM calls and no network: backfill.invoke_flush is stubbed (a "success" deletes the file,
as flush.py does). SCRIPTS_DIR, the attempts file and preserved-failed/ are redirected into a
temp dir per case, so nothing touches the real captures in scripts/.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import retry_preserved as rp  # noqa: E402

FAILURES: list[str] = []
SID = "01b71f93-0d45-4a74-a8c0-f8bfd9a24993"


def check(label: str, condition: bool) -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)


def _setup(tmp: Path, case: str, files: dict[str, float]) -> Path:
    """A fresh scripts dir holding `files` ({name: age in seconds}). Returns the dir."""
    d = tmp / case
    d.mkdir()
    for name, age in files.items():
        p = d / name
        p.write_text("a conversation worth keeping", encoding="utf-8")
        t = time.time() - age
        os.utime(p, (t, t))
    rp.SCRIPTS_DIR = d
    rp.ATTEMPTS_FILE = d / "preserved-retries.json"
    rp.FAILED_DIR = d / "preserved-failed"
    return d


def _run(succeed: bool) -> tuple[int, list[tuple[str, str, str]], str]:
    """Run main() once. Returns (rc, [(file, session_id, date) tried], printed output)."""
    calls: list[tuple[str, str, str]] = []

    def _stub(context_file: Path, session_id: str, target_date: str) -> bool:
        calls.append((context_file.name, session_id, target_date))
        if succeed:
            context_file.unlink()
        return succeed

    rp.backfill.invoke_flush = _stub
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = rp.main()
    return rc, calls, out.getvalue()


def _name(n: int) -> str:
    return f"backfill-{SID}-2026-09-{n:02d}-202609{n:02d}-120000.md"


HOUR = 3600


def main() -> int:
    print("name parsing - all three producers carry the ORIGINAL date")
    check("backfill", rp.parse_name(f"backfill-{SID}-2026-09-28-20260928-180331.md") == (SID, "2026-09-28"))
    check("session-flush", rp.parse_name(f"session-flush-{SID}-20260923-041216.md") == (SID, "2026-09-23"))
    check("flush-context", rp.parse_name(f"flush-context-{SID}-20261001-235959.md") == (SID, "2026-10-01"))
    check("unrecognised -> None", rp.parse_name("backfill-nodate.md") is None)

    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)

        print("a file younger than an hour is not touched (a live flush may own it)")
        d = _setup(tmp, "young", {_name(1): 60, _name(2): 2 * HOUR})
        rc, calls, out = _run(succeed=True)
        check("only the old file was tried", [c[0] for c in calls] == [_name(2)])
        check("the young file is still there", (d / _name(1)).exists())

        print("success: oldest first, file gone, count falls, right day")
        d = _setup(tmp, "ok", {_name(5): 2 * HOUR, _name(3): 5 * HOUR})
        rc, calls, out = _run(succeed=True)
        check("oldest first", [c[0] for c in calls] == [_name(3), _name(5)])
        check("routed to the capture's own date", calls[0][1:] == (SID, "2026-09-03"))
        check("no capture left", not list(d.glob("backfill-*.md")))
        check("rc 0", rc == 0)
        check("summary counts", "2 saved, 0 failed, 0 set aside, 0 still waiting" in out)

        print("failure: the run STOPS and the attempt is counted")
        d = _setup(tmp, "fail", {_name(1): 5 * HOUR, _name(2): 2 * HOUR})
        rc, calls, out = _run(succeed=False)
        check("only one tried", len(calls) == 1)
        check("attempt 1 recorded", json.loads(rp.ATTEMPTS_FILE.read_text())[_name(1)] == 1)
        check("both files kept", (d / _name(1)).exists() and (d / _name(2)).exists())
        check("rc 1 - no false green", rc == 1)
        check("summary counts", "0 saved, 1 failed, 0 set aside, 2 still waiting" in out)

        print("third failed run MOVES the file aside - never deletes it")
        _run(succeed=False)
        rc, calls, out = _run(succeed=False)
        check("moved to preserved-failed/", (d / "preserved-failed" / _name(1)).exists())
        check("gone from scripts/", not (d / _name(1)).exists())
        check("its count is dropped", _name(1) not in json.loads(rp.ATTEMPTS_FILE.read_text()))
        check("summary counts", "1 set aside, 1 still waiting" in out)
        rc, calls, out = _run(succeed=True)
        check("next run moves on to the next file", [c[0] for c in calls] == [_name(2)])

        print("at most 10 per run")
        d = _setup(tmp, "cap", {_name(n): (20 - n) * HOUR for n in range(1, 13)})
        rc, calls, out = _run(succeed=True)
        check("10 tried", len(calls) == 10)
        check("2 still waiting", "2 still waiting" in out)

        print("an unrecognised name is left alone and does not use up a slot")
        d = _setup(tmp, "odd", {"backfill-nodate.md": 5 * HOUR, _name(1): 2 * HOUR})
        rc, calls, out = _run(succeed=True)
        check("only the parseable file tried", [c[0] for c in calls] == [_name(1)])
        check("odd file still there", (d / "backfill-nodate.md").exists())
        check("odd file not counted as waiting", "1 saved, 0 failed, 0 set aside, 0 still waiting" in out)

    print(f"\n{'ALL PASS' if not FAILURES else f'{len(FAILURES)} FAILED: {FAILURES}'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
