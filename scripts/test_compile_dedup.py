"""Tests for the anti-duplication mechanisms in compile.py.

Run:  uv run --directory <project> python scripts/test_compile_dedup.py

No LLM calls, no network, no filesystem writes. Every assertion runs against an
in-memory fixture, NOT the real wiki: knowledge/ is gitignored, so a test that asserts
against the live corpus passes only on this machine and goes red the moment someone
actually collapses a duplicate pile - failing for doing the right thing. The live
corpus is only ever REPORTED here, never asserted on.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from compile import select_log_content, state_entry  # noqa: E402
from utils import duplicate_pairs_among, near_duplicate_pairs  # noqa: E402

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
# must describe the bytes that were COMPILED, not a fresh read of a file that has grown.
mid = b"appended while the compile was still running\n"
check("text appended DURING a compile is compiled on the next run",
      select_log_content(base + mid, state_entry(base, 0.0)), (mid.decode(), True))
check("state_entry describes the compiled bytes, not the grown file",
      (state_entry(base, 1.25)["compiled_bytes"], state_entry(base, 1.25)["hash"]),
      (len(base), h(base)))

# Demonstrate that the OLD way lost it, so this fails loudly if anyone reverts to
# re-reading the file: a record covering base+mid makes the next run start PAST mid.
later = b"text written the following day\n"
old_style = {"hash": h(base + mid), "compiled_bytes": len(base + mid)}
got, _ = select_log_content(base + mid + later, old_style)
check("old-style record SKIPS the mid-compile text (the bug, demonstrated)",
      mid.decode() in got, False)
check("...and only the later text survives, so the skip is unrecoverable",
      got, later.decode())

print("\nduplicate_pairs_among - exact expected pairs on a FIXTURE, not the live wiki")
# Two known real shapes plus one unrelated article. The unrelated one is the control:
# without it, a detector that simply paired everything would still look correct.
fixture = [
    "concepts/ssrf-unvalidated-urlopen-scheme.md",     # exact permutation of the next
    "concepts/urlopen-ssrf-unvalidated-scheme.md",
    "concepts/one-knowledge-plane-program.md",         # subset-of-a-longer-slug shape
    "concepts/one-knowledge-plane-program-ini-018.md",
    "concepts/vault-secret-rotation-policy.md",        # control: must pair with nothing
]
pairs = duplicate_pairs_among(fixture)
got_set = {frozenset((a, b)) for a, b, _ in pairs}
want_set = {
    frozenset(("concepts/ssrf-unvalidated-urlopen-scheme.md",
               "concepts/urlopen-ssrf-unvalidated-scheme.md")),
    frozenset(("concepts/one-knowledge-plane-program.md",
               "concepts/one-knowledge-plane-program-ini-018.md")),
}
# Asserting the EXACT set is what makes this meaningful: an earlier version only checked
# that two matching names appeared SOMEWHERE in the output, which would have passed even
# if each was paired with an unrelated article and the real pile went undetected.
check("finds exactly the two duplicate pairs and no others", got_set, want_set)
check("an exact word-permutation scores 1.0",
      next(round(o, 2) for a, b, o in pairs if "ssrf" in a), 1.0)
check("the unrelated control article appears in no pair",
      any("vault-secret" in n for a, b, _ in pairs for n in (a, b)), False)

print("\nduplicate_pairs_among - prove it can report CLEAN (else a low count means nothing)")
check("a corpus with no duplicates returns no pairs",
      duplicate_pairs_among(["concepts/alpha-one.md",
                             "concepts/beta-two.md",
                             "concepts/gamma-three.md"]), [])
check("a single article cannot pair with itself",
      duplicate_pairs_among(["concepts/lonely-article-here.md"]), [])
check("threshold is honoured - raising it past the score drops the pair",
      duplicate_pairs_among(["concepts/one-knowledge-plane-program.md",
                             "concepts/one-knowledge-plane-program-ini-018.md"],
                            threshold=0.95), [])

# Reported, never asserted - the live corpus is machine-specific and is meant to shrink.
print(f"\n  INFO  live corpus currently reports {len(near_duplicate_pairs())} near-duplicate pairs")

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")
