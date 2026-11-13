"""Test: a log whose child never answers hits the per-log limit (WI-bugfix-3f7b01).

Run:  python scripts/test_compile_log_timeout.py   (no LLM, no network)
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import compile as c  # noqa: E402

failures: list[str] = []


def check(label, ok):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        failures.append(label)


closed = []
saved_states = []


class Msg:  # stands in for ResultMessage of the good log
    total_cost_usd = 0.5
    is_error = False
    subtype = "success"


def fake_sdk(hang_name):
    mod = types.ModuleType("claude_agent_sdk")
    mod.AssistantMessage = type("AssistantMessage", (), {})
    mod.TextBlock = type("TextBlock", (), {})
    mod.ClaudeAgentOptions = lambda **kw: kw
    mod.ResultMessage = Msg

    async def query(prompt, options):
        try:
            if hang_name in prompt:
                await asyncio.sleep(3600)  # child that never answers
            yield Msg()
        finally:
            closed.append(hang_name in prompt)  # cleanup ran = child stopped

    mod.query = query
    return mod


def main():
    tmp = Path(tempfile.mkdtemp())
    hung, good = tmp / "2026-01-01.md", tmp / "2026-01-02.md"
    hung.write_text("hung-log-marker", encoding="utf8")
    good.write_text("fine", encoding="utf8")
    sys.modules["claude_agent_sdk"] = fake_sdk("hung-log-marker")
    c.LOG_TIMEOUT_S = 0.3
    c.build_prompt = lambda p, text, note: (text, 0, 0)
    c.save_state = lambda s: saved_states.append(1)
    state: dict = {}
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        r1 = asyncio.run(c.compile_daily_log(hung, state))
        r2 = asyncio.run(c.compile_daily_log(good, state))
    text = out.getvalue()
    check("hung log prints TIMEOUT <file>", f"TIMEOUT {hung.name}" in text)
    check("hung log returns not-compiled", r1[1] is False)
    check("hung log left out of state", hung.name not in state.get("ingested", {}))
    check("child cleanup ran for the hung log", closed and closed[0] is True)
    check("next log still compiles", r2[1] is True and good.name in state["ingested"])


main()
print(f"\nOK: {len(failures)} failure(s)" if not failures else f"\nFAILED: {failures}")
sys.exit(1 if failures else 0)
