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
import hashlib
import sys
from pathlib import Path

from config import AGENTS_FILE, CONCEPTS_DIR, CONNECTIONS_DIR, DAILY_DIR, KNOWLEDGE_DIR, now_iso
from utils import (
    file_hash,
    list_raw_files,
    list_wiki_articles,
    load_state,
    near_duplicate_pairs,
    read_wiki_index,
    save_state,
)

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
        and hashlib.sha256(raw[:n]).hexdigest()[:16] == prior_hash
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
        "hash": hashlib.sha256(raw).hexdigest()[:16],
        "compiled_bytes": len(raw),
        "compiled_at": now_iso(),
        "cost_usd": cost,
    }


async def compile_daily_log(log_path: Path, state: dict) -> float:
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
    prev_entry = (state.get("ingested") or {}).get(log_path.name) or {}
    log_content, is_increment = select_log_content(raw_bytes, prev_entry)
    increment_note = (
        "  **INCREMENT** - this day was already compiled. What follows is ONLY the text"
        " appended since. Everything before it ALREADY HAS ARTICLES: update those, never"
        " re-create them under a new title."
        if is_increment
        else ""
    )
    schema = AGENTS_FILE.read_text(encoding="utf-8")
    wiki_index = read_wiki_index()

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
    article_paths = [str(p.relative_to(KNOWLEDGE_DIR)) for p in list_wiki_articles()]
    existing_articles_context = (
        "\n".join(f"- {rel}" for rel in article_paths)
        if article_paths
        else "(No existing articles yet)"
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

## Current Wiki Index

{wiki_index}

## Existing Wiki Articles (paths only, relative to `knowledge/`)

{existing_articles_context}

Their contents are deliberately NOT included here. Before you update or link to any
article, open it with the Read tool. Use Grep to find which articles already mention
a concept. Only read the articles this daily log actually touches - do not read them all.

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

2. **For EACH concept, SEARCH BEFORE YOU WRITE.** Grep `knowledge/concepts/` for the
   concept's distinctive words, and scan the article-path list above. Search for
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

    cost = 0.0

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
            ),
        ):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        pass  # compilation output - LLM writes files directly
            elif isinstance(message, ResultMessage):
                cost = message.total_cost_usd or 0.0
                print(f"  Cost: ${cost:.4f}")
    except Exception as e:
        print(f"  Error: {e}")
        return 0.0

    # Update state
    rel_path = log_path.name
    state.setdefault("ingested", {})[rel_path] = state_entry(raw_bytes, cost)
    state["total_cost"] = state.get("total_cost", 0.0) + cost
    save_state(state)

    return cost


def main():
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
        return

    print(f"{'[DRY RUN] ' if args.dry_run else ''}Files to compile ({len(to_compile)}):")
    for f in to_compile:
        print(f"  - {f.name}")

    if args.dry_run:
        return

    # Compile each file sequentially
    total_cost = 0.0
    for i, log_path in enumerate(to_compile, 1):
        print(f"\n[{i}/{len(to_compile)}] Compiling {log_path.name}...")
        cost = asyncio.run(compile_daily_log(log_path, state))
        total_cost += cost
        print(f"  Done.")

    articles = list_wiki_articles()
    print(f"\nCompilation complete. Total cost: ${total_cost:.2f}")
    print(f"Knowledge base: {len(articles)} articles")

    # Duplication is otherwise invisible: nobody notices a synonym pile until someone
    # counts files by hand. Printing the count on every run makes it a trend the
    # nightly log carries, so a regression shows up as a rising number, and a fix
    # has something to be measured against (WI-bugfix-3cdd9f).
    dupes = near_duplicate_pairs()
    print(f"Near-duplicate article pairs: {len(dupes)}")
    for name_a, name_b, overlap in dupes[:5]:
        print(f"  {overlap:.2f}  {name_a}  <->  {name_b}")


if __name__ == "__main__":
    main()
