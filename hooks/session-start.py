"""
SessionStart hook - injects knowledge base context into every conversation.

This is the "context injection" layer. When Claude Code starts a session,
this hook reads the knowledge base index and recent daily log, then injects
them as additional context so Claude always "remembers" what it has learned.

Configure in .claude/settings.json:
{
    "hooks": {
        "SessionStart": [{
            "matcher": "",
            "command": "uv run python hooks/session-start.py"
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

MAX_CONTEXT_CHARS = 20_000
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


def build_context() -> str:
    """Assemble the context to inject into the conversation.

    Priority order (WI-bugfix-1e55f0): today's date and the recent daily log
    are ALWAYS present; the index gets whatever budget remains and is compacted
    explicitly rather than tail-truncated silently.
    """
    today = datetime.now(timezone.utc).astimezone()
    today_part = f"## Today\n{today.strftime('%A, %B %d, %Y')}"

    recent_log = get_recent_log()
    log_part = f"## Recent Daily Log\n\n{recent_log}"

    sep = "\n\n---\n\n"
    index_budget = MAX_CONTEXT_CHARS - len(today_part) - len(log_part) - 3 * len(sep) - 40

    if INDEX_FILE.exists():
        index_content = INDEX_FILE.read_text(encoding="utf-8")
        if len(index_content) > index_budget:
            index_content = compact_index(index_content, index_budget)
        index_part = f"## Knowledge Base Index\n\n{index_content}"
    else:
        index_part = "## Knowledge Base Index\n\n(empty - no articles compiled yet)"

    context = sep.join([today_part, log_part, index_part])

    # Hard backstop only — compact_index should keep us under on its own.
    if len(context) > MAX_CONTEXT_CHARS:
        context = context[:MAX_CONTEXT_CHARS] + "\n\n...(truncated)"

    return context


def main():
    context = build_context()

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }

    print(json.dumps(output))


if __name__ == "__main__":
    main()
