"""
Compile daily conversation logs into structured knowledge articles.

This is the "LLM compiler" - it reads daily logs (source code) and produces
organized knowledge articles (the executable).

Usage:
    uv run python compile.py                    # compile new/changed logs only
    uv run python compile.py --all              # force recompile everything
    uv run python compile.py --file daily/2026-04-01.md  # compile a specific log
    uv run python compile.py --dry-run          # show what would be compiled
"""

from __future__ import annotations

import argparse
import asyncio
import faulthandler
import os
import signal
import sys
import time
from pathlib import Path

from config import AGENTS_FILE, CONCEPTS_DIR, CONNECTIONS_DIR, DAILY_DIR, KNOWLEDGE_DIR, now_iso
from utils import (
    file_hash,
    hash_bytes,
    list_raw_files,
    list_wiki_articles,
    load_state,
    near_duplicate_pairs,
    read_wiki_index,
    save_state,
    select_related,
)

# Mark every nested Claude session this script starts as machine-invoked, BEFORE the
# SDK is imported or spawned. hooks/session-end.py and hooks/pre-compact.py both bail
# out when they see this, exactly as flush.py:16 and backfill.py:251 already do.
#
# WHY (found in review of d921e51): compile.py spawns an Agent-SDK session whose cwd is
# this project, so .claude/settings.json applies to it and its SessionEnd hook fires.
# Repairing those hook paths made that hook WORK, which re-armed a loop compile.py had
# never needed a guard for: compile -> session-end.py -> flush.py -> flush summarises
# the COMPILER'S OWN transcript into today's daily log -> maybe_trigger_compilation()
# spawns compile.py again. Observed live at 08:32:41 on 2026-09-26, during this very
# script's run (scripts/flush.log, session 4d358782). It stopped there only because the
# hour was before COMPILE_AFTER_HOUR; at the 22:00 nightly slot it would not have.
# Besides the wasted work, it feeds the compiler's self-description into the wiki as
# articles - manufacturing the duplication this changeset exists to reduce.
os.environ["CLAUDE_INVOKED_BY"] = "memory_compile"

# ── Paths for the LLM to use ──────────────────────────────────────────
ROOT_DIR = Path(__file__).resolve().parent.parent


def select_log_content(raw: bytes, prev: dict) -> tuple[str, bool]:
    """Decide what text to hand the compiler for one daily log.

    A daily log GROWS through the day - sessions append to it - and the pending check
    is hash-based, so every append makes the whole file look new and the WHOLE log is
    recompiled from scratch. Each pass re-extracts concepts it has already written and,
    wording them slightly differently each time, mints near-identical articles instead
    of updating the existing ones. Measured 2026-09-25: 2026-09-25.md was compiled
    THREE times in one day and produced three articles for a single idea -
      "pm2 resurrect Ships the Current Working Tree, Not a Point-in-Time Snapshot"
      "pm2 resurrect Ships Whatever Is Currently on Disk, Not a Code Snapshot"
      "pm2 resurrect Deploys Current Disk State, Not a Frozen Snapshot"
    all three citing that one source log, two written a SECOND apart. Corpus-wide that
    mechanism has produced 101 near-duplicate pairs (WI-bugfix-3cdd9f).

    So when a log has only been APPENDED to, hand over just the new tail and say so.
    `prev["hash"]` is the hash of the file exactly as it was last compiled, so
    re-hashing the first `compiled_bytes` bytes is a sufficient append test - no new
    stored digest is needed. Byte offsets are safe to slice at because that prefix was
    itself a complete, valid UTF-8 file when it was compiled.

    Anything else - edited, truncated, rewritten, or compiled before `compiled_bytes`
    existed - falls back to a full recompile, which is the old behaviour.

    Returns (text_to_compile, is_increment).
    """
    n = prev.get("compiled_bytes")
    prior_hash = prev.get("hash")
    if (
        isinstance(n, int)
        and prior_hash
        and 0 < n < len(raw)
        and hash_bytes(raw[:n]) == prior_hash
    ):
        return raw[n:].decode("utf-8"), True
    return raw.decode("utf-8"), False


def state_entry(raw: bytes, cost: float) -> dict:
    """Build the ingested-state record for content that was JUST compiled.

    Both fields describe `raw` - the bytes actually handed to the compiler - and NOT a
    fresh read of the file. This matters: a compile takes ~90 seconds and sessions
    append to the daily log the whole time, so the file on disk is usually LONGER by
    the time this runs. Re-reading it here (which is what this did until 2026-09-26)
    records a hash and a length covering text that was never compiled, and
    select_log_content() then starts the next run PAST it - so whatever was appended
    DURING a compile would never be compiled at all.

    That was harmless before incremental compiling existed: a hash that disagreed with
    the file just meant "recompile the whole log", which redid work but lost nothing.
    With the tail optimisation it becomes silent knowledge loss, which is the one
    failure this whole change must not introduce.
    """
    return {
        "hash": hash_bytes(raw),
        "compiled_bytes": len(raw),
        "compiled_at": now_iso(),
        "cost_usd": cost,
    }


def build_prompt(log_path: Path, log_content: str, increment_note: str) -> tuple[str, int, int]:
    """The compile prompt for one log; returns (prompt, articles sent, articles in the wiki).

    Separate from compile_daily_log() so its size can be measured without an LLM call.
    """
    schema = AGENTS_FILE.read_text(encoding="utf-8")

    # List existing articles by PATH ONLY - never inline their contents.
    #
    # This used to read every article's full text into the prompt. That made the
    # prompt grow linearly with the knowledge base (1.58 MB across 324 articles by
    # 2026-07-26) and, because it is re-sent on all 30 turns, drove compile cost to
    # ~$5 per daily log REGARDLESS of the log's size - a 118-byte log on 2026-07-16
    # cost $4.82. Cost was also quadratic overall: each new article made every
    # future compile more expensive.
    #
    # The agent has Read/Grep/Glob, so it opens the handful of articles a given log
    # actually touches instead of receiving all of them.
    #
    # The same now applies to the index rows and the path LIST themselves
    # (WI-bugfix-9fbb94): only the related ones are sent - see select_related().
    # as_posix() so the paths match the index's `[[concepts/slug]]` rows on every OS.
    all_paths = [p.relative_to(KNOWLEDGE_DIR).as_posix() for p in list_wiki_articles()]
    index_rows, related = select_related(
        log_content, read_wiki_index(), all_paths, source_name=log_path.name
    )
    if related:
        related_context = (
            f"These are {len(related)} of the wiki's {len(all_paths)} articles: those already"
            f" compiled from {log_path.name}, then those whose NAMES share the most distinctive"
            " words with this log. It is a pre-selected SUBSET, not the whole wiki.\n\n"
            "### Their index rows\n\n"
            + ("\n".join(index_rows) if index_rows
               else "(None of these articles has a row in index.md yet.)")
            + "\n\n### Their paths (relative to `knowledge/`)\n\n"
            + "\n".join(f"- {rel}" for rel in related)
        )
    else:
        related_context = (
            f"None of the wiki's {len(all_paths)} articles share a distinctive word with this"
            " log. That does NOT mean the topic is new - search before creating anything."
        )

    timestamp = now_iso()

    # The task rules below are ordered SEARCH-FIRST, not create-first, and say so in
    # bold. They used to read "2. Create concept articles ... 4. Update existing
    # articles IF ...", which made creating the default and updating a conditional
    # afterthought. Combined with the (correct, cost-driven) instruction above not to
    # read every article, the agent had no obligation to look before writing, so a
    # recurring topic got a fresh file under a reshuffled title instead of an update.
    # Measured 2026-09-25 across 603 articles: 101 pairs sharing >=60% of their title
    # words and 8 exact word-for-word permutations, e.g.
    # `ssrf-unvalidated-urlopen-scheme` / `urlopen-ssrf-unvalidated-scheme`. INI-018
    # alone had six articles describing one programme. That is a recall-quality
    # problem, not a disk problem - a lookup returns six articles saying the same
    # thing and neither reader nor agent can tell which is current. See
    # WI-bugfix-3cdd9f. Collapsing the ALREADY-duplicated pairs is deliberately NOT
    # done here; it belongs at retrieval time (INI-018 U8). This stops the growth.
    prompt = f"""You are a knowledge compiler. Your job is to read a daily conversation log
and extract knowledge into structured wiki articles.

## Schema (AGENTS.md)

{schema}

## Related Wiki Articles

{related_context}

Their contents are deliberately NOT included here. Before you update or link to any
article, open it with the Read tool. Use Grep to find which articles already mention
a concept. Only read the articles this daily log actually touches - do not read them all.

**Do NOT Read `knowledge/index.md` in full**, although the schema's compile steps say to
read it first: the related rows above replace that step, and the whole index (500+ rows)
would be re-sent on every later turn. To add your rows, Read only its first 10 lines and
insert below the table header; to check one article's row, Grep the index for its slug.

## Daily Log to Compile

**File:** {log_path.name}{increment_note}

{log_content}

## Your Task

Read the daily log above and compile it into wiki articles following the schema exactly.

### Rules:

1. **Extract key concepts, then CONSOLIDATE your list BEFORE writing anything.**
   Identify 3-7 concepts worth their own article. Then re-read your own list and MERGE
   any two that are the same idea worded differently. If two titles would share most of
   their significant words they are ONE concept - for example "X Ships the Current
   Working Tree", "X Ships Whatever Is Currently on Disk" and "X Deploys Current Disk
   State" are one article, not three. Writing near-identical articles within a single
   pass is the LARGEST source of duplication in this wiki, and searching cannot catch
   it, because neither article exists yet at the moment you plan them both.

2. **For EACH concept, SEARCH BEFORE YOU WRITE.** Grep `knowledge/concepts/`,
   `knowledge/connections/`, `knowledge/projects/` and `knowledge/qa/` (not index.md or
   log.md) for the concept's distinctive words, and scan the related paths above - they
   are a SUBSET picked by name, so the Grep is what finds the rest. Search for
   REORDERED and SYNONYMOUS forms of the title, not just the exact slug you have in
   mind - the same topic has repeatedly been filed twice under permuted names, e.g.
   `ssrf-unvalidated-urlopen-scheme` alongside `urlopen-ssrf-unvalidated-scheme`.
   - **An article on this concept already exists -> UPDATE IT IN PLACE.** Read it, merge
     the new information into its existing sections, and add this daily log to
     `sources:`. Do NOT write a second article on a topic that already has one, and
     never create a file whose title is a reordering or near-synonym of an existing one.
   - **Nothing matches -> create a new article** in `knowledge/concepts/`, one .md file
     per concept.

3. **Article format** - applies whether you created or updated the article
   - Use the exact article format from AGENTS.md (YAML frontmatter + sections)
   - Include `sources:` in frontmatter pointing to the daily log file
   - Use `[[concepts/slug]]` wikilinks to link to related concepts
   - Write in encyclopedia style - neutral, comprehensive

4. **Create connection articles** in `knowledge/connections/` if this log reveals non-obvious
   relationships between 2+ existing concepts
5. **Update knowledge/index.md** - Add new entries to the table
   - Each entry: `| [[path/slug]] | One-line summary | source-file | {timestamp[:10]} |`
6. **Append to knowledge/log.md** - Add a timestamped entry:
   ```
   ## [{timestamp}] compile | {log_path.name}
   - Source: daily/{log_path.name}
   - Articles created: [[concepts/x]], [[concepts/y]]
   - Articles updated: [[concepts/z]] (if any)
   ```

### File paths:
- Write concept articles to: {CONCEPTS_DIR}
- Write connection articles to: {CONNECTIONS_DIR}
- Update index at: {KNOWLEDGE_DIR / 'index.md'}
- Append log at: {KNOWLEDGE_DIR / 'log.md'}

### Quality standards:
- Every article must have complete YAML frontmatter
- Every article must link to at least 2 other articles via [[wikilinks]]
- Key Points section should have 3-5 bullet points
- Details section should have 2+ paragraphs
- Related Concepts section should have 2+ entries
- Sources section should cite the daily log with specific claims extracted
"""
    return prompt, len(related), len(all_paths)


async def compile_daily_log(log_path: Path, state: dict, force: bool = False) -> float:
    """Compile a single daily log into knowledge articles.

    Returns the API cost of the compilation.
    """
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )

    raw_bytes = log_path.read_bytes()
    # `--all` and `--file` are the documented recovery levers ("Force recompile all
    # logs"), so they must mean the WHOLE log, not the tail. Discarding prev_entry is
    # what makes select_log_content fall back to a full compile; without this, a
    # rebuild after the wiki is damaged would silently send only appended text and
    # report success (found in review of d921e51).
    prev_entry = {} if force else ((state.get("ingested") or {}).get(log_path.name) or {})
    log_content, is_increment = select_log_content(raw_bytes, prev_entry)
    # The note must also OVERRIDE rule 1's "3-7 concepts" floor. An increment can be a
    # few hundred characters, and a floor applied to it forces the agent to pad one
    # throwaway remark into three articles - each a reworded facet of something the
    # earlier full compile already wrote. That would manufacture exactly the
    # near-duplicates this change exists to prevent (found in review of d921e51).
    increment_note = (
        "  **INCREMENT** - this day was already compiled. What follows is ONLY the text"
        " appended since. Everything before it ALREADY HAS ARTICLES: update those, never"
        " re-create them under a new title. **This OVERRIDES the 3-7 concept count in"
        " rule 1: for an increment the right number is 0-3, and ZERO IS A CORRECT AND"
        " COMMON ANSWER.** A short tail usually warrants updating one existing article"
        " and nothing else. Do not invent articles to reach a quota; if the appended"
        " text adds no new concept, update what exists, note that in knowledge/log.md,"
        " and stop."
        if is_increment
        else ""
    )
    prompt, n_sent, n_total = build_prompt(log_path, log_content, increment_note)
    # The deterministic half of WI-bugfix-9fbb94's check: cost per run swings with
    # turn count (one log ran 3-69 turns), the prompt size does not.
    print(f"  Prompt: {n_sent} of {n_total} articles, {len(prompt):,} chars")

    cost = 0.0
    incomplete = False

    try:
        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                cwd=str(ROOT_DIR),
                # Pin the model explicitly. This was previously unset, so it inherited the
                # SDK default (Opus) - the most expensive tier - for what is structured
                # authoring, not frontier reasoning. Sonnet 5 is ~2.5x cheaper at current
                # pricing with comparable quality on this kind of agentic file work.
                # Pinning also stops a future SDK default from silently moving our costs.
                model="claude-sonnet-5",
                # Hard per-file ceiling. NOT a billing guard - this runs on a Max
                # subscription (no ANTHROPIC_API_KEY in any scope; ~/.claude/.credentials
                # .json carries claudeAiOauth subscriptionType=max), so the figure is the
                # API-EQUIVALENT cost of the tokens, not money charged. What it actually
                # bounds is Max rate-limit capacity consumed by an unattended 22:00 job:
                # the 2026-07-26 runaway burned $45.31-equivalent, which shows up not as a
                # bill but as the owner being throttled mid-morning for no visible reason.
                # It is also this job's only regression detector - nothing else watches it.
                #
                # Raised 2.00 -> 4.00 on 2026-09-25. The old value's premise ("~6x headroom,
                # never trips in normal operation") was measured when the corpus was 324
                # articles and a log cost ~$0.33. At 645 articles normal operation is ~$2
                # (the prompt carries every article path, re-sent across up to 30 turns), so
                # the ceiling had started aborting real work: 2026-09-22 (18.7 KB) failed
                # twice at $2.24 and $1.88, while 2026-09-25 succeeded at $1.99 and failed
                # at $2.06 - the boundary, not the content, decided the outcome. 4.00 keeps
                # ~2x headroom over current normal, still tight enough to catch a runaway.
                # This treats the SYMPTOM. The cause - cost decoupled from log size as the
                # corpus grows - is tracked separately; see the prompt-size comment above.
                max_budget_usd=4.00,
                system_prompt={"type": "preset", "preset": "claude_code"},
                allowed_tools=["Read", "Write", "Edit", "Glob", "Grep"],
                permission_mode="acceptEdits",
                max_turns=30,
                # WHY (WI-bugfix-e78c0f): scheduled runs died with NO output at all, and
                # the CLI's stderr is only piped when a callback is registered. Printed as
                # it arrives, not as a tail at the end like flush.py, because a run killed
                # mid-flight never reaches an end; run_compile.bat sets PYTHONUNBUFFERED so
                # these lines survive that kill.
                stderr=lambda line: print(f"  [cli] {line}"),
            ),
        ):
            if isinstance(message, AssistantMessage):
                if message.error:
                    # The real reason for an opaque exit-1 arrives here (WI-bugfix-4d7e21
                    # found usage-limit and CLI-too-old this way). Logged only; the
                    # ResultMessage check below still decides what is recorded.
                    print(f"  API error ({message.error})")
                for block in message.content:
                    if isinstance(block, TextBlock):
                        pass  # compilation output - LLM writes files directly
            elif isinstance(message, ResultMessage):
                cost = message.total_cost_usd or 0.0
                print(f"  Cost: ${cost:.4f}")
                # A run can END BADLY WITHOUT RAISING: hitting max_turns yields a
                # ResultMessage with subtype "error_max_turns" and exit code 0, so no
                # exception reaches the handler below. Recording that as compiled used
                # to be self-healing - state stored a re-read hash that no longer
                # matched, so the next run redid the whole log and picked up the
                # concepts the agent never got to. With incremental compiling the
                # stored prefix matches exactly, the next run sends only the tail, and
                # the unprocessed middle of the log is never compiled by anyone.
                # So: an incomplete result must NOT be recorded as a complete compile.
                if message.is_error or message.subtype != "success":
                    print(f"  Incomplete result ({message.subtype}) —"
                          " leaving this log pending so it is retried in full")
                    incomplete = True
    except Exception as e:
        # The ResultMessage often arrives BEFORE the failure, so `cost` is usually
        # already set: returning 0.0 here threw away a real, already-incurred figure
        # and total_cost silently under-reported (WI-bugfix-1fac6f: $3.94-equivalent
        # unaccounted on 2026-09-25). Bank what was spent; still do NOT write an
        # "ingested" record, so the log stays pending and is retried in full.
        print(f"  Error: {e}")
        state["total_cost"] = state.get("total_cost", 0.0) + cost
        save_state(state)
        return cost

    # The capacity was consumed either way, so always bank it - an incomplete run that
    # reported a cost must still show up in the ledger (WI-bugfix-1fac6f).
    state["total_cost"] = state.get("total_cost", 0.0) + cost

    if incomplete:
        # Deliberately do NOT write an "ingested" record: leaving the log pending means
        # the next run recompiles it IN FULL, which is the recovery path. Writing one
        # here would tell select_log_content() the whole file was handled and the
        # unprocessed part would be skipped permanently.
        save_state(state)
        return cost

    # Update state
    rel_path = log_path.name
    state.setdefault("ingested", {})[rel_path] = state_entry(raw_bytes, cost)
    save_state(state)

    return cost


def report_duplicates() -> None:
    """Print the near-duplicate trend line.

    Called on EVERY exit path, including "nothing to compile" and --dry-run. The point
    of the number is to be a trend the nightly log carries, and the common steady-state
    night compiles nothing at all - so printing it only after real work left gaps in
    scheduled_compile.log exactly where a flat baseline is most informative.
    """
    dupes = near_duplicate_pairs()
    print(f"Near-duplicate article pairs: {len(dupes)}")
    for name_a, name_b, overlap in dupes[:5]:
        print(f"  {overlap:.2f}  {name_a}  <->  {name_b}")


def stamp() -> str:
    return time.strftime("%H:%M:%S")


def report_deaths() -> None:
    """Make every death this process CAN see leave a line in the log.

    WHY (WI-bugfix-e78c0f): the 2026-10-13 22:00 run died mid-file with [exit=1] and
    nothing else, even with the CLI's stderr logged. `taskkill /F` reproduces exactly
    that (rc=1, no output): a hard kill runs no handler at all, and neither does
    SIGKILL on Linux. What this adds is the deaths that DO announce themselves - a
    stop/hang-up signal (SIGTERM/SIGHUP from cron, systemd or a container stop on
    Linux and cloud hosts; SIGBREAK on Windows) and a hard crash (faulthandler). So
    the next silent death narrows to "killed from outside"; a death with a line names
    the signal.

    Line-buffered here, not only via run_compile.bat's PYTHONUNBUFFERED: a Linux
    cron/systemd job has no such wrapper, and a hard kill discards whatever is still
    buffered - exactly the lines that would place the death.
    """
    sys.stdout.reconfigure(line_buffering=True)
    faulthandler.enable()

    def on_signal(signum, _frame):
        print(f"  [signal {signal.Signals(signum).name} at {stamp()}] exiting")
        sys.exit(128 + signum)

    for name in ("SIGTERM", "SIGHUP", "SIGBREAK"):
        sig = getattr(signal, name, None)  # SIGHUP is POSIX-only, SIGBREAK Windows-only
        if sig is not None:
            signal.signal(sig, on_signal)
    print(f"[run pid={os.getpid()} started {stamp()}]")


def hold_system_awake() -> bool:
    """Ask Windows to keep this run alive until it exits; a no-op elsewhere.

    WHY (WI-bugfix-e78c0f, owner option A 2026-10-16): on 2026-10-15 the PC sat in
    Modern Standby from 19:24, so the 22:00 run started in a seconds-long maintenance
    wake at 22:42:51 and was gone 20 s later - [exit=1], no line, no Claude call. The
    owner wants the job done FROM standby without changing the schedule, so once
    Windows lets it start it must be allowed to finish.

    What does the work is SystemRequired: "the system continues to run instead of
    entering sleep after a period of user inactivity" (MS Learn, PowerSetRequest). It
    is system-wide, so it also covers the claude CLI child the SDK spawns.
    ExecutionRequired is taken too, but its "process lifetime management" is the
    packaged-app lifecycle, so for this plain Win32 process it is most likely a no-op.
    This is an EXPERIMENT, not a proven fix: the 10-15 python returned exit 1 - it was
    ended, not suspended - and nothing here targets whatever ended it; it only keeps
    the machine out of the standby it died in. Not covered: the seconds before main()
    (cmd + uv start-up). Documented limits: the requests end on lid close / power
    button / Start->Sleep, and on DC power 5 minutes after the sleep timeout. The
    reason string shows in `powercfg /requests` (needs an admin shell).

    Process exit releases the request anyway; atexit clears it first, as the docs ask.
    Never fatal: any failure is logged and the compile carries on as before.
    """
    if sys.platform != "win32":
        return False  # cron/systemd/cloud hosts have no Modern Standby to fight
    try:
        return _take_power_requests()
    except Exception as e:  # ctypes/loader errors must not cost the night's compile
        print(f"  [keep-awake failed: {type(e).__name__}: {e}]")
        return False


def _take_power_requests() -> bool:
    """The Windows half of hold_system_awake(); raises on a ctypes/loader error."""
    import atexit
    import ctypes
    from ctypes import wintypes

    class _Detailed(ctypes.Structure):
        _fields_ = [("LocalizedReasonModule", wintypes.HMODULE),
                    ("LocalizedReasonId", wintypes.ULONG),
                    ("ReasonStringCount", wintypes.ULONG),
                    ("ReasonStrings", ctypes.POINTER(wintypes.LPWSTR))]

    class _Reason(ctypes.Union):
        _fields_ = [("Detailed", _Detailed), ("SimpleReasonString", wintypes.LPWSTR)]

    class REASON_CONTEXT(ctypes.Structure):
        _fields_ = [("Version", wintypes.ULONG), ("Flags", wintypes.DWORD), ("Reason", _Reason)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.PowerCreateRequest.restype = wintypes.HANDLE
    k32.PowerCreateRequest.argtypes = [ctypes.POINTER(REASON_CONTEXT)]
    k32.PowerSetRequest.argtypes = k32.PowerClearRequest.argtypes = [wintypes.HANDLE, ctypes.c_int]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    SYSTEM_REQUIRED, EXECUTION_REQUIRED = 1, 3  # POWER_REQUEST_TYPE

    ctx = REASON_CONTEXT(Version=0, Flags=0x1)  # POWER_REQUEST_CONTEXT_SIMPLE_STRING
    ctx.Reason.SimpleReasonString = "Brain OS memory compile"
    handle = k32.PowerCreateRequest(ctypes.byref(ctx))
    if not handle or handle == wintypes.HANDLE(-1).value:
        print(f"  [keep-awake refused: PowerCreateRequest error {ctypes.get_last_error()}]")
        return False
    held, errors = [], []
    for t in (SYSTEM_REQUIRED, EXECUTION_REQUIRED):
        if k32.PowerSetRequest(handle, t):
            held.append(t)
        else:
            errors.append(f"type {t}: error {ctypes.get_last_error()}")  # read at the failing call

    def release():
        for t in held:
            k32.PowerClearRequest(handle, t)
        k32.CloseHandle(handle)

    atexit.register(release)
    if len(held) < 2:
        print(f"  [keep-awake partial: {len(held)} of 2 requests, {'; '.join(errors)}]")
    else:
        print("  [keep-awake held: system + execution required until exit]")
    return len(held) == 2


def main():
    report_deaths()
    hold_system_awake()  # first thing: the 10-15 run was killed 15 s after it started
    parser = argparse.ArgumentParser(description="Compile daily logs into knowledge articles")
    parser.add_argument("--all", action="store_true", help="Force recompile all logs")
    parser.add_argument("--file", type=str, help="Compile a specific daily log file")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be compiled")
    args = parser.parse_args()

    state = load_state()

    # Determine which files to compile
    if args.file:
        target = Path(args.file)
        if not target.is_absolute():
            target = DAILY_DIR / target.name
        if not target.exists():
            # Try resolving relative to project root
            target = ROOT_DIR / args.file
        if not target.exists():
            print(f"Error: {args.file} not found")
            sys.exit(1)
        to_compile = [target]
    else:
        all_logs = list_raw_files()
        if args.all:
            to_compile = all_logs
        else:
            to_compile = []
            for log_path in all_logs:
                rel = log_path.name
                prev = state.get("ingested", {}).get(rel, {})
                if not prev or prev.get("hash") != file_hash(log_path):
                    to_compile.append(log_path)

    if not to_compile:
        print("Nothing to compile - all daily logs are up to date.")
        report_duplicates()
        return

    print(f"{'[DRY RUN] ' if args.dry_run else ''}Files to compile ({len(to_compile)}):")
    for f in to_compile:
        print(f"  - {f.name}")

    if args.dry_run:
        report_duplicates()
        return

    # Compile each file sequentially
    total_cost = 0.0
    for i, log_path in enumerate(to_compile, 1):
        print(f"\n[{i}/{len(to_compile)}] {stamp()} Compiling {log_path.name}...")
        cost = asyncio.run(
            compile_daily_log(log_path, state, force=bool(args.all or args.file))
        )
        total_cost += cost
        print(f"  Done. ({stamp()})")

    articles = list_wiki_articles()
    print(f"\nCompilation complete. Total cost: ${total_cost:.2f}")
    print(f"Knowledge base: {len(articles)} articles")

    # Duplication is otherwise invisible: nobody notices a synonym pile until someone
    # counts files by hand. Printing the count on every run makes it a trend the
    # nightly log carries, so a regression shows up as a rising number, and a fix
    # has something to be measured against (WI-bugfix-3cdd9f).
    report_duplicates()


if __name__ == "__main__":
    main()
