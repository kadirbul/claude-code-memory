"""
SessionStart hook - injects knowledge base context into every conversation.

This is the "context injection" layer. When Claude Code starts a session,
this hook reads the knowledge base index and recent daily log, then injects
them as additional context so Claude always "remembers" what it has learned.

It runs as TWO hooks, one per part, because Claude Code caps each hook's
additionalContext at 10,000 characters and replaces anything longer with a
2,000-char preview (WI-bugfix-3adda1). Configure in .claude/settings.json:
{
    "hooks": {
        "SessionStart": [{
            "matcher": "",
            "hooks": [
                {"type": "command", "command": "uv run python hooks/session-start.py --part log"},
                {"type": "command", "command": "uv run python hooks/session-start.py --part index"}
            ]
        }]
    }
}
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Paths relative to project root
ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = ROOT / "knowledge"
DAILY_DIR = ROOT / "daily"
INDEX_FILE = KNOWLEDGE_DIR / "index.md"

# Per PART, not per session: Claude Code measures each hook's additionalContext
# on its own and caps it at 10,000 chars (code.claude.com/docs/en/hooks, "JSON
# output"; no setting raises it). 20_000 here meant every session got only a
# 2K preview - date and log top, no index at all (WI-bugfix-3adda1).
MAX_CONTEXT_CHARS = 9_800
MAX_LOG_LINES = 30

# WI-bugfix-1e55f0 (knowledge-plane audit 2026-09-07, F6): the old code injected
# the FULL index (109KB) then blind tail-truncated the whole context to 20KB —
# silently dropping ~80% of the index AND the entire Recent Daily Log section
# (it was last). Now the log is guaranteed, and the index is compacted to fit
# the remaining budget: newest FULL_ROWS rows kept whole (recency carries the
# most value), the rest as title-only lines, with an honest "N of M shown"
# footer so the model knows to Read knowledge/index.md for the remainder.
FULL_ROWS = 25


def get_recent_log() -> str:
    """Read the most recent daily log (today or yesterday)."""
    today = datetime.now(timezone.utc).astimezone()

    for offset in range(2):
        date = today - timedelta(days=offset)
        log_path = DAILY_DIR / f"{date.strftime('%Y-%m-%d')}.md"
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8").splitlines()
            # Return last N lines to keep context small
            recent = lines[-MAX_LOG_LINES:] if len(lines) > MAX_LOG_LINES else lines
            return "\n".join(recent)

    return "(no recent daily log)"


def compact_index(index_text: str, budget: int) -> str:
    """Fit the index into `budget` chars without silent loss.

    Newest FULL_ROWS table rows are kept whole; the rest become title-only
    lines ("- [[name]] (updated)") until the budget runs out; a footer states
    exactly how many of how many entries are shown and where the full index
    lives. The table is newest-first, so 'first rows' == most recent articles.
    """
    rows = [ln for ln in index_text.splitlines() if ln.startswith("| [[")]
    total = len(rows)
    if not rows:
        return index_text[:budget]

    out: list[str] = ["| Article | Summary | Updated |", "|---|---|---|"]
    used = sum(len(l) + 1 for l in out)
    shown_full = 0
    shown_title = 0

    footer_reserve = 220  # keep room for the footer line no matter what

    for i, row in enumerate(rows):
        cells = [c.strip() for c in row.strip("|").split("|")]
        # cells: [ [[link]], summary, compiled-from, updated ]
        link = cells[0] if cells else row
        summary = cells[1] if len(cells) > 1 else ""
        updated = cells[-1] if len(cells) > 3 else ""
        if i < FULL_ROWS:
            line = f"| {link} | {summary} | {updated} |"
        else:
            line = f"- {link} ({updated})"
        if used + len(line) + 1 > budget - footer_reserve:
            break
        out.append(line)
        used += len(line) + 1
        if i < FULL_ROWS:
            shown_full += 1
        else:
            shown_title += 1

    shown = shown_full + shown_title
    out.append("")
    out.append(
        f"_(index compacted: {shown_full} recent entries in full + {shown_title} "
        f"title-only, of {total} total. The full index is `knowledge/index.md` — "
        f"Read it, or use `scripts/query.py`, when a topic isn't listed above.)_"
    )
    return "\n".join(out)


def build_context(part: str) -> str:
    """Assemble one part of the context to inject into the conversation.

    "log": today's date + the recent daily log; if the log alone overruns the
    budget its OLDEST lines are cut, since the newest carry the most value.
    "index": the knowledge index, compacted explicitly to the budget rather than
    tail-truncated silently (WI-bugfix-1e55f0). Each part is its own hook so
    each gets the full cap (WI-bugfix-3adda1).
    """
    if part == "log":
        today = datetime.now(timezone.utc).astimezone()
        today_part = f"## Today\n{today.strftime('%A, %B %d, %Y')}"
        head = "\n\n---\n\n## Recent Daily Log\n\n"
        recent_log = get_recent_log()
        room = MAX_CONTEXT_CHARS - len(today_part) - len(head)
        if len(recent_log) > room:
            cut_note = "...(earlier lines cut)\n"
            tail = recent_log[-(room - len(cut_note)):]
            # Drop the partial first line so the cut lands on a line boundary.
            recent_log = cut_note + (tail.split("\n", 1)[1] if "\n" in tail else tail)
        return today_part + head + recent_log

    index_budget = MAX_CONTEXT_CHARS - 40

    if INDEX_FILE.exists():
        index_content = INDEX_FILE.read_text(encoding="utf-8")
        if len(index_content) > index_budget:
            index_content = compact_index(index_content, index_budget)
        index_part = f"## Knowledge Base Index\n\n{index_content}"
    else:
        index_part = "## Knowledge Base Index\n\n(empty - no articles compiled yet)"

    # Hard backstop only — compact_index should keep us under on its own. The
    # marker is counted INSIDE the budget, or the backstop itself breaks the cap.
    if len(index_part) > MAX_CONTEXT_CHARS:
        marker = "\n\n...(truncated)"
        index_part = index_part[: MAX_CONTEXT_CHARS - len(marker)] + marker

    return index_part


def main():
    # Fail loudly: a silent default would hide a settings typo as a missing part.
    args = sys.argv[1:]
    part = args[args.index("--part") + 1] if args[-1:] != ["--part"] and "--part" in args else None
    if part not in ("log", "index"):
        print("session-start.py: needs --part log or --part index (WI-bugfix-3adda1)", file=sys.stderr)
        sys.exit(1)
    context = build_context(part)

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }

    print(json.dumps(output))


if __name__ == "__main__":
    main()
