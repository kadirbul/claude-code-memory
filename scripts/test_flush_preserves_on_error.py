"""Tests for WI-bugfix-4d7e21: a FAILED flush must preserve its input, not consume it.

Run:  uv run --directory <project> python scripts/test_flush_preserves_on_error.py

Before this fix, the FLUSH_ERROR branch appended the error text to the daily log (where
compile.py later read it as knowledge), then fell through to record the session as flushed
and unlink the context file. A transient SDK failure therefore became permanent silent
loss: 1525 error stanzas across 142 daily logs, 12 days whose log held nothing else, and a
22-session backfill on 2026-09-25 deleted outright.

No LLM calls and no network: run_flush is stubbed. Nothing touches the real daily/ or
last-flush.json - DAILY_DIR and STATE_FILE are redirected into a temp dir, so this cannot
pollute the corpus it is protecting. The happy paths are asserted too, because a guard that
also breaks a successful flush is not a fix.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import flush  # noqa: E402

# flush.py calls logging.basicConfig(filename=flush.log) at import. Drop the handler so a
# test run does not append to the diagnostic log we read to investigate real failures.
logging.getLogger().handlers = []
logging.getLogger().addHandler(logging.NullHandler())

FAILURES: list[str] = []


def check(label: str, condition: bool) -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)


def _run(monkeyed_response: str, tmp: Path, *, session_id: str = "sess-1",
         prior_state: dict | None = None) -> dict:
    """Drive flush.main() once with run_flush stubbed to return `monkeyed_response`.

    Returns what the run left behind: whether the context file survived, the dedup state,
    and the daily-log text (empty string when no log was written at all).
    """
    # One daily dir and state file PER CASE. Sharing them let an earlier case's daily log
    # satisfy a later case's "log is empty" assertion, which is how a green suite lies.
    daily = tmp / f"daily-{session_id}"
    daily.mkdir(exist_ok=True)
    state_file = tmp / f"last-flush-{session_id}.json"
    ctx = tmp / f"ctx-{session_id}.md"
    ctx.write_text("a conversation worth keeping", encoding="utf-8")
    if prior_state is not None:
        state_file.write_text(json.dumps(prior_state), encoding="utf-8")
    elif state_file.exists():
        state_file.unlink()

    flush.DAILY_DIR = daily
    flush.STATE_FILE = state_file

    async def _stub(_context: str) -> str:
        return monkeyed_response

    flush.run_flush = _stub
    flush.maybe_trigger_compilation = lambda: None          # never spawn a real compile
    sys.argv = ["flush.py", str(ctx), session_id, "2026-01-01"]
    flush.main()

    logs = sorted(daily.glob("*.md"))
    return {
        "context_survived": ctx.exists(),
        "state": json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {},
        "log_text": logs[0].read_text(encoding="utf-8") if logs else "",
    }


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        print("\nFLUSH_ERROR - the capture must survive, unrecorded and unlogged")
        r = _run("FLUSH_ERROR: Exception: Command failed with exit code 1", tmp)
        check("context file still on disk (the capture is recoverable)", r["context_survived"])
        check("session NOT recorded as flushed, so the 30-min window does not block a retry",
              "sess-1" not in (r["state"].get("sessions") or {}))
        check("daily log gained NOTHING - no error text for compile.py to read as knowledge",
              "FLUSH_ERROR" not in r["log_text"])
        check("no daily log written at all on a failed flush", r["log_text"] == "")

        print("\nprove the FLUSH_ERROR assertions can FAIL - the old behaviour must trip them")
        # Simulate the pre-fix branch on the same fixture: append, record, unlink. If these
        # assertions did not fire here, the four above would be vacuous.
        daily2 = tmp / "daily2"
        daily2.mkdir()
        state2 = tmp / "state2.json"
        ctx2 = tmp / "ctx-old.md"
        ctx2.write_text("a conversation worth keeping", encoding="utf-8")
        flush.DAILY_DIR, flush.STATE_FILE = daily2, state2
        flush.append_to_daily_log("FLUSH_ERROR: Exception: boom", "Memory Flush",
                                  target_date="2026-01-01")
        flush.save_flush_state({"sessions": {"sess-old": 1.0}, "last_session_id": "sess-old"})
        ctx2.unlink()
        old_logs = sorted(daily2.glob("*.md"))
        old_text = old_logs[0].read_text(encoding="utf-8") if old_logs else ""
        check("old behaviour DID delete the capture (so the survival check is meaningful)",
              not ctx2.exists())
        check("old behaviour DID record the session (so the retry check is meaningful)",
              "sess-old" in json.loads(state2.read_text(encoding="utf-8"))["sessions"])
        check("old behaviour DID write the error into the daily log (so that check is meaningful)",
              "FLUSH_ERROR" in old_text)

        print("\na SUCCESSFUL flush is unchanged - the guard must not break the happy path")
        r = _run("Learned that the vault scope was wrong.", tmp, session_id="sess-2")
        check("context file consumed on success", not r["context_survived"])
        check("session recorded on success", "sess-2" in (r["state"].get("sessions") or {}))
        check("content written to the daily log", "vault scope was wrong" in r["log_text"])

        print("\nFLUSH_OK (nothing worth saving) is unchanged")
        r = _run("FLUSH_OK", tmp, session_id="sess-3")
        check("context file consumed", not r["context_survived"])
        check("session recorded", "sess-3" in (r["state"].get("sessions") or {}))
        check("marker written to the daily log", "FLUSH_OK" in r["log_text"])

        print("\ndedup still short-circuits a repeat within the window")
        import time as _t
        r = _run("should never be reached", tmp, session_id="sess-4",
                 prior_state={"sessions": {"sess-4": _t.time()}, "last_session_id": "sess-4"})
        check("a duplicate inside the window consumes the context and writes nothing",
              not r["context_survived"] and r["log_text"] == "")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
