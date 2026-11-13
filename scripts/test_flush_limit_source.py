"""Test: only a real API rate-limit error records a pause; model text in a failed run does not.

Run:  .venv/Scripts/python.exe scripts/test_flush_limit_source.py   (no LLM, no network)
"""
from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import flush  # noqa: E402
import utils  # noqa: E402

logging.getLogger().handlers = [logging.NullHandler()]
FAILURES: list[str] = []


def check(label, ok):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        FAILURES.append(label)


def run(response: str, tmp: Path, n: str) -> bool:
    """Drive flush.main() with a stubbed run_flush; True when a pause file was written."""
    utils.RATE_LIMIT_FILE = tmp / f"limit-{n}.json"
    flush.DAILY_DIR = tmp / f"d{n}"
    flush.DAILY_DIR.mkdir()
    flush.STATE_FILE = tmp / f"s{n}.json"
    ctx = tmp / f"c{n}.md"
    ctx.write_text("x", encoding="utf-8")

    async def stub(_c):
        return response

    flush.run_flush = stub
    flush.maybe_trigger_compilation = lambda: None
    sys.argv = ["flush.py", str(ctx), f"sess-{n}", "2026-01-01"]
    flush.main()
    return utils.RATE_LIMIT_FILE.exists()


with tempfile.TemporaryDirectory() as t:
    t = Path(t)
    check("model text saying rate_limit/resets 11pm does NOT record a pause",
          not run("FLUSH_ERROR: API result error_max_turns: we discussed rate_limit, resets 11pm", t, "1"))
    check("a real API rate-limit error still records a pause",
          run("FLUSH_ERROR: API rate_limit: You're out of extra usage · resets 2am", t, "2"))

print(f"\nOK: {len(FAILURES)} failure(s)" if not FAILURES else f"\nFAILED: {FAILURES}")
sys.exit(1 if FAILURES else 0)
