"""Tests for the two anti-duplication mechanisms in compile.py.

Run:  uv run --directory <project> python scripts/test_compile_dedup.py

No LLM calls, no network, no writes outside a temp dir. `select_log_content` is a
pure function precisely so the expensive behaviour it governs can be tested for free.
"""
from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from compile import select_log_content  # noqa: E402
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


print("select_log_content — the append case it exists for")
base = b"line one\nline two\n"
tail = b"line three\n"
prev = {"hash": h(base), "compiled_bytes": len(base)}
check("appended log -> only the tail is sent",
      select_log_content(base + tail, prev), (tail.decode(), True))

print("\nselect_log_content — the cases where it MUST NOT take the shortcut")
# Proving it can fail is the point: each of these would silently drop real content
# if the guard were wrong, so each is checked explicitly rather than assumed.
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
      select_log_content("ünïcødé\n".encode() + tail,
                         {"hash": h("ünïcødé\n".encode()),
                          "compiled_bytes": len("ünïcødé\n".encode())}),
      (tail.decode(), True))

print("\nnear_duplicate_pairs — prove it DETECTS the known piles before trusting a low count")
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
