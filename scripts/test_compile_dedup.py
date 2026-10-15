"""Tests for the anti-duplication mechanisms in compile.py.

Run:  uv run --directory <project> python scripts/test_compile_dedup.py

No LLM calls, no network, no filesystem writes. Every assertion runs against an
in-memory fixture, NOT the real wiki: knowledge/ is gitignored, so a test that asserts
against the live corpus passes only on this machine and goes red the moment someone
actually collapses a duplicate pile - failing for doing the right thing. The live
corpus is only ever REPORTED here, never asserted on.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from compile import select_log_content, state_entry  # noqa: E402
from utils import duplicate_pairs_among, hash_bytes, near_duplicate_pairs, select_related  # noqa: E402

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}\n          got:  {got!r}\n          want: {want!r}")
        failures.append(label)


def h(b: bytes) -> str:
    return hash_bytes(b)


print("hash_bytes - pinned, because every state.json hash depends on it")
# Every check below builds BOTH sides with the same helper, so a changed digest or
# prefix length would leave them all green. What it really breaks is the hashes
# already stored in state.json: none match, and every log recompiles in full. Only a
# fixed value can catch that (WI-bugfix-2c38be). sha256(b"abc") is the FIPS 180-2 vector.
check("hash_bytes(b'abc') is sha256, first 16 hex chars", hash_bytes(b"abc"), "ba7816bf8f01cfea")

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

print("\nselect_related - only the related index rows and paths reach the prompt (WI-bugfix-9fbb94)")
paths = [
    "concepts/pm2-resurrect-ships-working-tree.md",
    "concepts/vault-secret-rotation-policy.md",          # control: unrelated to the log
    "concepts/pm2-cluster-mode.md",                      # one word shared, ranks lower
]
index = "\n".join([
    "| Article | Summary | Compiled From | Updated |",
    "|---|---|---|---|",
    "| [[concepts/pm2-resurrect-ships-working-tree]] | NEWEST row | d2 | 2026-10-02 |",
    "| [[concepts/vault-secret-rotation-policy]] | vault row | d1 | 2026-10-01 |",
    "| [[concepts/pm2-resurrect-ships-working-tree]] | older row | d1 | 2026-10-01 |",
    "| [[concepts/pm2-cluster-mode]] | cluster row | d1 | 2026-10-01 |",
])
log = "After the restart, pm2 resurrect shipped the working tree, not a snapshot."
rows, chosen = select_related(log, index, paths)
check("the best match ranks first, the one-word match second, the control not at all",
      chosen, ["concepts/pm2-resurrect-ships-working-tree.md", "concepts/pm2-cluster-mode.md"])
check("rows = table header, then each chosen article's NEWEST row only",
      rows, [index.splitlines()[0], index.splitlines()[1], index.splitlines()[2], index.splitlines()[5]])
check("limit is honoured", select_related(log, index, paths, limit=1)[1],
      ["concepts/pm2-resurrect-ships-working-tree.md"])
check("a log sharing no word with any name selects nothing (and sends no bare header)",
      select_related("zebra quokka", index, paths), ([], []))
check("stopwords alone never select an article",
      select_related("the and for with", index, ["concepts/the-and-for-with-x.md"]), ([], []))

# The cases the 2026-10-15 review found the first version getting wrong.
reordered = "\n".join([
    "| Article | Summary | Compiled From | Updated |",
    "|---|---|---|---|",
    "| [[concepts/pm2-cluster-mode]] | OLD summary | daily/2026-09-01.md | 2026-09-01 |",
    "| [[concepts/pm2-cluster-mode]] | NEW summary | daily/2026-10-14.md | 2026-10-14 |",
])
check("the newest row is chosen by its DATE, not by being first (the index is not always prepended)",
      select_related("pm2 cluster", reordered, ["concepts/pm2-cluster-mode.md"])[0][2:],
      [reordered.splitlines()[3]])
check("an INCREMENT tail sharing no word still gets the articles compiled from the same day",
      select_related("zebra quokka", reordered, ["concepts/pm2-cluster-mode.md"],
                     source_name="2026-10-14.md")[1],
      ["concepts/pm2-cluster-mode.md"])
common = [f"concepts/session-commit-fix-update-{i}-note.md" for i in range(20)]
check("a rare exact name outranks long names made of common words (IDF, not raw hits)",
      select_related("session commit fix update note about resurrect", "",
                     common + ["concepts/resurrect.md"], limit=1)[1],
      ["concepts/resurrect.md"])
check("one tokenizer for log and name: a dotted, mixed-case name is still found",
      select_related("Upgraded NextJS to v14.2 today", "",
                     ["concepts/NextJS-v14.2-build.md", "concepts/vault-secret-rotation-policy.md"])[1],
      ["concepts/NextJS-v14.2-build.md"])

# Reported, never asserted - the live corpus is machine-specific and is meant to shrink.
print(f"\n  INFO  live corpus currently reports {len(near_duplicate_pairs())} near-duplicate pairs")

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")
