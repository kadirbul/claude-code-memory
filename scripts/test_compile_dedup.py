"""Tests for the anti-duplication mechanisms in compile.py.

Run:  uv run --directory <project> python scripts/test_compile_dedup.py

No LLM calls, no network, no writes outside a temp dir. `select_log_content` and
`state_entry` are pure functions precisely so the expensive behaviour they govern can
be tested for free.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from compile import select_log_content, state_entry  # noqa: E402
from utils import near_duplicate_pairs  # noqa: E402

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}\n          got:  {got!r}\n          want: {want!r}")
        failures.append(label)


def h(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


base = b"line one\nline two\n"
tail = b"line three\n"

print("select_log_content - the append case it exists for")
prev = {"hash": h(base), "compiled_bytes": len(base)}
check("appended log -> only the tail is sent",
      select_log_content(base + tail, prev), (tail.decode(), True))

print("\nselect_log_content - the cases where it MUST NOT take the shortcut")
# Proving it can fail is the point: each of these would silently drop real content if
# the guard were wrong, so each is checked explicitly rather than assumed.
check("no prior state -> full recompile",
      select_log_content(base + tail, {}), ((base + tail).decode(), False))
check("legacy entry with no compiled_bytes -> full recompile",
      select_log_content(base + tail, {"hash": h(base)}),
      ((base + tail).decode(), False))
check("prefix EDITED (hash mismatch) -> full recompile, tail NOT trusted",
      select_log_content(b"line ONE changed\nline two\n" + tail, prev),
      ((b"line ONE changed\nline two\n" + tail).decode(), False))
check("log truncated shorter than compiled_bytes -> full recompile",
      select_log_content(b"short\n", prev), ("short\n", False))
check("log unchanged (nothing appended) -> full recompile, never an empty prompt",
      select_log_content(base, prev), (base.decode(), False))
check("multi-byte char across the boundary stays intact",
      select_log_content("unicode-üïø\n".encode() + tail,
                         {"hash": h("unicode-üïø\n".encode()),
                          "compiled_bytes": len("unicode-üïø\n".encode())}),
      (tail.decode(), True))

print("\nstate_entry - the mid-compile append the old code silently dropped")
# A compile takes ~90s and sessions append to the daily log throughout it. The record
# must therefore describe the bytes that were COMPILED, not a fresh read of a file that
# has grown since. Recording the re-read is what the code did until 2026-09-26.
mid = b"appended while the compile was still running\n"
check("text appended DURING a compile is compiled on the next run",
      select_log_content(base + mid, state_entry(base, 0.0)), (mid.decode(), True))

# And demonstrate that the OLD way lost it, so this fails loudly if anyone reverts to
# re-reading the file: a record covering base+mid makes the next run start PAST mid, so
# that text is never compiled by anyone, ever.
later = b"text written the following day\n"
old_style = {"hash": h(base + mid), "compiled_bytes": len(base + mid)}
got, _ = select_log_content(base + mid + later, old_style)
check("old-style record SKIPS the mid-compile text (the bug, demonstrated)",
      mid.decode() in got, False)
check("...and only the later text survives, so the skip is unrecoverable",
      got, later.decode())
check("state_entry describes the compiled bytes, not the grown file",
      (state_entry(base, 1.25)["compiled_bytes"], state_entry(base, 1.25)["hash"]),
      (len(base), h(base)))

print("\nnear_duplicate_pairs - prove it DETECTS the known piles before trusting a low count")
pairs = near_duplicate_pairs()
names = {(a, b) for a, b, _ in pairs}
flat = {n for a, b, _ in pairs for n in (a, b)}


def detects(sub: str) -> bool:
    return sum(1 for n in flat if sub in n) >= 2


check("finds the pm2-resurrect pile", detects("pm2-resurrect"), True)
check("finds the baseline-preserving pile", detects("baseline-preserving"), True)
check("finds the one-knowledge-plane pile", detects("one-knowledge-plane"), True)
check("a pair is never an article against itself",
      any(a == b for a, b in names), False)
print(f"  INFO  {len(pairs)} near-duplicate pairs currently in the corpus")

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")
