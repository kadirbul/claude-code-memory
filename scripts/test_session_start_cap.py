"""Tests for WI-bugfix-3adda1: each session-start part fits Claude Code's 10,000-char hook cap.

Run:  uv run --directory <project> python scripts/test_session_start_cap.py

Claude Code caps a hook's additionalContext at 10,000 characters; anything longer is saved
to a file and Claude sees a 2,000-char preview only (code.claude.com/docs/en/hooks, JSON
output). No LLM calls: INDEX_FILE and DAILY_DIR are redirected into a temp dir per case.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "session-start.py"
_spec = importlib.util.spec_from_file_location("session_start", HOOK)
ss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ss)

CLAUDE_CODE_CAP = 10_000
FAILURES: list[str] = []


def check(label: str, condition: bool) -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        FAILURES.append(label)


def _fixture(tmp: Path, index_rows: int, log_lines: int, line_len: int) -> None:
    """Point the hook at a temp index of `index_rows` rows and a log for today."""
    rows = "\n".join(
        f"| [[concepts/article-{i:04d}]] | {'summary text ' * 12} | daily/2026-10-01.md | 2026-10-15 |"
        for i in range(index_rows)
    )
    index = tmp / "index.md"
    index.write_text("| Article | Summary | Compiled From | Updated |\n|---|---|---|---|\n" + rows,
                     encoding="utf-8")
    daily = tmp / "daily"
    daily.mkdir()
    lines = [f"line-{i:03d} " + "x" * line_len for i in range(log_lines)]
    (daily / f"{datetime.now().astimezone().strftime('%Y-%m-%d')}.md").write_text(
        "\n".join(lines), encoding="utf-8")
    ss.INDEX_FILE = index
    ss.DAILY_DIR = daily


def main() -> int:
    real_index, real_daily, real_compact = ss.INDEX_FILE, ss.DAILY_DIR, ss.compact_index

    print("real data: both parts fit the cap")
    for part in ("log", "index"):
        n = len(ss.build_context(part))
        check(f"{part} part is {n} chars <= {CLAUDE_CODE_CAP}", n <= CLAUDE_CODE_CAP)

    with tempfile.TemporaryDirectory() as t:
        print("oversized index (600 rows) and oversized log (30 lines x 800 chars)")
        _fixture(Path(t), index_rows=600, log_lines=30, line_len=800)
        log = ss.build_context("log")
        check(f"log part {len(log)} chars <= cap", len(log) <= CLAUDE_CODE_CAP)
        check("log part keeps today's date", log.startswith("## Today"))
        check("log part keeps the NEWEST line (line-029)", "line-029" in log)
        after_cut = log.split("...(earlier lines cut)\n", 1)[-1]
        check("log cut lands on a line boundary", after_cut.startswith("line-"))
        index = ss.build_context("index")
        check(f"index part {len(index)} chars <= cap", len(index) <= CLAUDE_CODE_CAP)
        check("index part carries the 'N of M shown' footer", "of 600 total" in index)
        check("index part leaves the log out", "line-0" not in index)

        print("backstop: compact_index overshooting still cannot break the cap")
        ss.compact_index = lambda text, budget: "y" * 50_000
        index = ss.build_context("index")
        check(f"index part {len(index)} chars <= cap", len(index) <= CLAUDE_CODE_CAP)
        ss.compact_index = real_compact

    ss.INDEX_FILE, ss.DAILY_DIR = real_index, real_daily

    print("a missing or wrong --part fails loudly instead of silently picking a part")
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    for argv in ([], ["--part"], ["--part", "indx"]):
        rc = subprocess.run([sys.executable, str(HOOK), *argv], capture_output=True,
                            creationflags=no_window).returncode
        check(f"{argv or 'no args'} exits non-zero (rc={rc})", rc != 0)

    print(f"\n{'ALL PASS' if not FAILURES else f'{len(FAILURES)} FAIL'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
