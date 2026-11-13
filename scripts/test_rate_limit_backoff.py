"""Tests for WI-bugfix-6733b8: retries wait for a usage-limit reset and do not burn attempts.

Run:  python scripts/test_rate_limit_backoff.py   (no LLM, no network)
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import retry_preserved as rp  # noqa: E402
import utils  # noqa: E402

FAILURES: list[str] = []


def check(label, ok):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        FAILURES.append(label)


SID = "01b71f93-0d45-4a74-a8c0-f8bfd9a24993"
NAME = f"backfill-{SID}-2026-09-01-20260901-120000.md"


def run(d: Path, succeed_after_limit: bool):
    rp.SCRIPTS_DIR, rp.ATTEMPTS_FILE, rp.FAILED_DIR = d, d / "a.json", d / "pf"
    calls = []

    def stub(f, sid, dt):
        calls.append(f.name)
        if succeed_after_limit:
            f.unlink()
        else:  # a failing flush that hit the limit records it, as flush.py does
            utils.record_rate_limit("API rate_limit: You've hit your limit · resets 8pm")
        return succeed_after_limit

    rp.backfill.invoke_flush = stub
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = rp.main()
    return rc, calls, out.getvalue()


def main():
    now = 1_800_000_000.0
    # 1. parsing
    t = utils.parse_reset_epoch("You're out of extra usage · resets 2am", now)
    check("reset time parsed, in the future, within 24h", t is not None and 0 < t - now <= 86400)
    check("hour is 2", datetime.fromtimestamp(t).hour == 2)
    check("no reset text -> None", utils.parse_reset_epoch("boom", now) is None)
    check("rate-limit text recognised", utils.is_rate_limit_error("FLUSH_ERROR: API rate_limit: x"))
    check("plain error not recognised", not utils.is_rate_limit_error("FLUSH_ERROR: Command failed"))

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        utils.RATE_LIMIT_FILE = tmp / "limit.json"
        check("no file -> 0 left", utils.rate_limit_seconds_left() == 0)

        # 2. a limit lasting hours ends the run, no call, no attempt counted
        d = tmp / "long"; d.mkdir()
        (d / NAME).write_text("x", encoding="utf-8")
        os.utime(d / NAME, (time.time() - 7200,) * 2)
        utils.RATE_LIMIT_FILE.write_text(json.dumps({"until": time.time() + 7200}))
        rc, calls, out = run(d, True)
        check("long limit: model not called", calls == [])
        check("long limit: capture kept", (d / NAME).exists())
        check("long limit: no attempt counted", not json.loads((d / "a.json").read_text()))

        # 3. a short limit is waited out, then the file succeeds
        slept = []
        rp.time = type("T", (), {"time": staticmethod(time.time), "sleep": staticmethod(slept.append)})
        utils.RATE_LIMIT_FILE.write_text(json.dumps({"until": time.time() + 60}))
        rc, calls, out = run(d, True)
        rp.time = time
        check("short limit: slept ~until reset", len(slept) == 1 and 60 <= slept[0] <= 70)
        check("short limit: then saved", calls == [NAME] and not (d / NAME).exists())

        # 4. a failure that hits the limit does not count an attempt
        d2 = tmp / "hit"; d2.mkdir()
        (d2 / NAME).write_text("x", encoding="utf-8")
        os.utime(d2 / NAME, (time.time() - 7200,) * 2)
        utils.RATE_LIMIT_FILE.unlink()
        rc, calls, out = run(d2, False)
        check("limit failure: attempt not counted", not json.loads((d2 / "a.json").read_text()))
        check("limit failure: rc 1, file kept", rc == 1 and (d2 / NAME).exists())

    print(f"\nOK: {len(FAILURES)} failure(s)" if not FAILURES else f"\nFAILED: {FAILURES}")
    return 1 if FAILURES else 0


raise SystemExit(main())
