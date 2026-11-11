"""Tests for the run's exit code when logs fail (WI-bugfix-e78c0f follow-up, 2026-11-11).

Run:  uv run --directory <project> python scripts/test_compile_exit_code.py

No LLM calls, no network. From 2026-11-07 every log failed with oauth_org_not_allowed,
yet each run printed "Compilation complete" and exited 0, so nothing looked wrong for
four days. A run with a failed log must say how many failed and exit 2 (exit 1 stays
the sign of a death, which report_deaths() and the death study read).
"""
from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import compile as c  # noqa: E402

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}\n          got:  {got!r}\n          want: {want!r}")
        failures.append(label)


def run_main(results: list[bool]) -> tuple[object, str]:
    """Run main() over len(results) fake logs; results[i] says whether log i compiled."""
    logs = [Path(f"2026-11-0{i + 1}.md") for i in range(len(results))]
    outcome = iter(results)

    async def fake_compile(log_path, state, force=False):
        return 0.0, next(outcome)

    saved = {n: getattr(c, n) for n in ("compile_daily_log", "list_raw_files", "load_state",
                                         "hold_system_awake", "report_deaths",
                                         "report_duplicates", "list_wiki_articles")}
    c.compile_daily_log = fake_compile
    c.list_raw_files = lambda: logs
    c.load_state = lambda: {}
    c.hold_system_awake = lambda: True
    c.report_deaths = lambda: None
    c.report_duplicates = lambda: None
    c.list_wiki_articles = lambda: []
    argv, sys.argv = sys.argv, ["compile.py"]
    out = io.StringIO()
    code: object = None
    try:
        with contextlib.redirect_stdout(out):
            c.main()
    except SystemExit as e:
        code = e.code
    finally:
        sys.argv = argv
        for n, f in saved.items():
            setattr(c, n, f)
    return code, out.getvalue()


print("all logs compiled")
code, out = run_main([True, True])
check("exit code is 0 (None)", code in (None, 0), True)
check("says 2 of 2 compiled", "2 of 2 log(s) compiled" in out, True)

print("every log failed (the 2026-11-07 oauth case)")
code, out = run_main([False, False, False])
check("exit code is 2", code, 2)
check("does not claim complete", "Compilation complete" in out, False)
check("names the failed count", "3 of 3 log(s) FAILED" in out, True)

print("one of two failed")
code, out = run_main([True, False])
check("exit code is 2", code, 2)
check("names the failed count", "1 of 2 log(s) FAILED" in out, True)

print(f"\n{'FAILED' if failures else 'OK'}: {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
