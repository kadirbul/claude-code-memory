"""
Regression tests for WI-bugfix-4d7e21 part 2 - run_flush() must report WHY the CLI failed.

The flush died 1,689 times as "Command failed with exit code 1 ... Check stderr output
for details", with the detail discarded. The CLI's own transcripts held the reason: an
AssistantMessage the SDK flags with `error` ("You've hit your limit", "Claude Code
2.1.131 does not support this model"). Two fixes were guesses because nobody could see it.

These cases drive the REAL run_flush() with only claude_agent_sdk.query faked, so they
exercise the classification inside it (test_flush_preserves_on_error.py stubs run_flush
whole and cannot see it). No LLM calls, no network.

Usage:
    uv run python scripts/test_flush_classifies_sdk_errors.py [path/to/flush.py]
The optional path runs the suite against another copy - used to prove it FAILS on the
pre-fix code before trusting it.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path

import claude_agent_sdk
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "flush.py"
spec = importlib.util.spec_from_file_location("flush_under_test", target)
flush = importlib.util.module_from_spec(spec)
spec.loader.exec_module(flush)

# flush.py calls logging.basicConfig(filename=flush.log) at import. Replace the handler so
# a test run does not append to the diagnostic log, and so we can read what it logged.
LOGGED: list[str] = []


class _Capture(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        LOGGED.append(record.getMessage())


logging.getLogger().handlers = [_Capture()]

FAILURES: list[str] = []
LIMIT = "You've hit your limit · resets 8pm (Europe/Istanbul)"
STDERR = ["Error: usage limit reached", "exiting with code 1"]


def check(label: str, condition: bool) -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)


def _assistant(text: str, error: str | None = None) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="<synthetic>" if error else "claude-haiku-4-5", error=error)


def _result() -> ResultMessage:
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                         num_turns=1, session_id="s")


def _run(messages: list, *, raise_after: Exception | None = None, stderr: list[str] | None = None):
    """Fake query(): emit `stderr` lines through the registered callback, yield
    `messages`, then optionally raise like the SDK does when the CLI exits non-zero."""
    seen_options = {}

    async def fake_query(*, prompt, options):
        seen_options["options"] = options
        for line in stderr or []:
            if getattr(options, "stderr", None):
                options.stderr(line)
        for m in messages:
            yield m
        if raise_after:
            raise raise_after

    LOGGED.clear()
    claude_agent_sdk.query = fake_query
    return asyncio.run(flush.run_flush("ctx")), seen_options.get("options")


def main() -> int:
    exit1 = Exception("Command failed with exit code 1 (exit code: 1)\nError output: Check stderr output for details")

    print("usage limit, CLI exits 1 (the dominant real case: 271 of 351)")
    resp, opts = _run([_assistant(LIMIT, "rate_limit")], raise_after=exit1, stderr=STDERR)
    check("classified as a failure", resp.startswith("FLUSH_ERROR:"))
    check("the failure names the real reason, not 'check stderr'", "hit your limit" in resp)
    check("the failure carries the SDK's error kind", "rate_limit" in resp)
    check("a stderr callback was registered, so the CLI's stderr is piped", getattr(opts, "stderr", None) is not None)
    check("the CLI's stderr reaches the log", any("usage limit reached" in m for m in LOGGED))

    print("\nusage limit, CLI exits 0 (how 2026-05-07 / 06-25 saved error text AS knowledge)")
    resp, _ = _run([_assistant(LIMIT, "rate_limit"), _result()])
    check("still classified as a failure", resp.startswith("FLUSH_ERROR:"))
    check("the error text is not returned as summary content", not resp.startswith("You've"))

    print("\nmodel too new for the bundled CLI (79 of 351)")
    msg = "API Error: 400 Claude Code 2.1.131 does not support this model; version 2.1.251 or newer is required."
    resp, _ = _run([_assistant(msg, "invalid_request")], raise_after=exit1)
    check("classified as a failure naming the version gap", resp.startswith("FLUSH_ERROR:") and "2.1.251" in resp)

    print("\nan exception with no error message (e.g. 'Control request timeout: initialize')")
    resp, _ = _run([], raise_after=Exception("Control request timeout: initialize"), stderr=["boom"])
    check("still a failure, carrying the exception text", resp.startswith("FLUSH_ERROR:") and "initialize" in resp)
    check("stderr is logged on this path too", any("boom" in m for m in LOGGED))

    print("\nthe happy paths are untouched - a guard that breaks a good flush is not a fix")
    resp, _ = _run([_assistant("**Context:** worked on X"), _result()], stderr=["harmless warning"])
    check("a real summary comes back verbatim", resp == "**Context:** worked on X")
    check("stderr is NOT logged as an error on success", not any("CLI stderr" in m for m in LOGGED))
    resp, _ = _run([_assistant("FLUSH_OK"), _result()])
    check("FLUSH_OK still comes back as FLUSH_OK", resp == "FLUSH_OK")

    print(f"\n{'ALL PASS' if not FAILURES else f'{len(FAILURES)} FAILED'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
