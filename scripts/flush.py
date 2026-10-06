"""
Memory flush agent - extracts important knowledge from conversation context.

Spawned by session-end.py or pre-compact.py as a background process. Reads
pre-extracted conversation context from a .md file, uses the Claude Agent SDK
to decide what's worth saving, and appends the result to today's daily log.

Usage:
    uv run python flush.py <context_file.md> <session_id>
"""

from __future__ import annotations

# Recursion prevention: set this BEFORE any imports that might trigger Claude
import os
os.environ["CLAUDE_INVOKED_BY"] = "memory_flush"

import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DAILY_DIR = ROOT / "daily"
SCRIPTS_DIR = ROOT / "scripts"
STATE_FILE = SCRIPTS_DIR / "last-flush.json"
LOG_FILE = SCRIPTS_DIR / "flush.log"

# Recursion-guard lock file. session-end.py checks for this — while it exists
# (and is fresh) no new flush is spawned, because the Claude Agent SDK call
# inside this flush starts its own Claude Code session whose SessionEnd hook
# would otherwise recurse. See session-end.py "Recursion guard" for the full
# story (the 2026-05-07 runaway: 5,094 flushes / 2 days).
FLUSH_LOCK = SCRIPTS_DIR / "flush.lock"

# Set up file-based logging so we can verify the background process ran.
# The parent process sends stdout/stderr to DEVNULL (to avoid the inherited
# file handle bug on Windows), so this is our only observability channel.
logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def load_flush_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_flush_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def append_to_daily_log(content: str, section: str = "Session", target_date: str | None = None) -> None:
    """Append content to a daily log.

    target_date — optional 'YYYY-MM-DD' to write into a specific day's log
    instead of today's. Used by backfill so historical sessions land in
    the correct daily file.
    """
    if target_date:
        try:
            parsed = datetime.strptime(target_date, "%Y-%m-%d")
            today = parsed.astimezone()
        except ValueError:
            today = datetime.now(timezone.utc).astimezone()
    else:
        today = datetime.now(timezone.utc).astimezone()
    log_path = DAILY_DIR / f"{today.strftime('%Y-%m-%d')}.md"

    if not log_path.exists():
        DAILY_DIR.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            f"# Daily Log: {today.strftime('%Y-%m-%d')}\n\n## Sessions\n\n## Memory Maintenance\n\n",
            encoding="utf-8",
        )

    time_str = today.strftime("%H:%M")
    entry = f"### {section} ({time_str})\n\n{content}\n\n"

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(entry)


async def run_flush(context: str) -> str:
    """Use Claude Agent SDK to extract important knowledge from conversation context."""
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )

    prompt = f"""Review the conversation context below and respond with a concise summary
of important items that should be preserved in the daily log.
Do NOT use any tools — just return plain text.

Format your response as a structured daily log entry with these sections:

**Context:** [One line about what the user was working on]

**Key Exchanges:**
- [Important Q&A or discussions]

**Decisions Made:**
- [Any decisions with rationale]

**Lessons Learned:**
- [Gotchas, patterns, or insights discovered]

**Action Items:**
- [Follow-ups or TODOs mentioned]

Skip anything that is:
- Routine tool calls or file reads
- Content that's trivial or obvious
- Trivial back-and-forth or clarification exchanges

Only include sections that have actual content. If nothing is worth saving,
respond with exactly: FLUSH_OK

## Conversation Context

{context}"""

    response = ""

    try:
        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                cwd=str(ROOT),
                allowed_tools=[],
                max_turns=2,
                # Pinned: unset inherited the account default (Opus) for a
                # 2-turn summarisation that Haiku handles fine.
                model="claude-haiku-4-5-20251001",
            ),
        ):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        response += block.text
            elif isinstance(message, ResultMessage):
                pass
    except Exception as e:
        import traceback
        logging.error("Agent SDK error: %s\n%s", e, traceback.format_exc())
        response = f"FLUSH_ERROR: {type(e).__name__}: {e}"

    return response


def main():
    # Usage: flush.py <context_file.md> <session_id> [target_date YYYY-MM-DD]
    # target_date is optional; backfill.py uses it to route historical
    # transcripts to the correct daily log.
    if len(sys.argv) < 3:
        logging.error("Usage: %s <context_file.md> <session_id> [target_date]", sys.argv[0])
        sys.exit(1)

    context_file = Path(sys.argv[1])
    session_id = sys.argv[2]
    target_date = sys.argv[3] if len(sys.argv) > 3 else None

    logging.info("flush.py started for session %s, context: %s", session_id, context_file)

    if not context_file.exists():
        logging.error("Context file not found: %s", context_file)
        return

    # Deduplication: skip if same session was flushed within the dedup window.
    # Window must be long enough that the Stop hook (which fires after every
    # assistant turn) doesn't spam the LLM, but short enough that long sessions
    # still get periodic snapshots. 30 min strikes the balance — combined with
    # MAX_TURNS=30 in session-end.py, each flush captures the most recent
    # ~30 turns, so a multi-hour session naturally gets several snapshots.
    DEDUP_WINDOW_S = 1800  # 30 minutes
    state = load_flush_state()
    sessions = state.get("sessions", {}) if isinstance(state.get("sessions"), dict) else {}
    last_ts = sessions.get(session_id, 0)
    if time.time() - last_ts < DEDUP_WINDOW_S:
        logging.info(
            "Skipping duplicate flush for session %s (last flushed %ds ago)",
            session_id, int(time.time() - last_ts),
        )
        context_file.unlink(missing_ok=True)
        return

    # Read pre-extracted context
    context = context_file.read_text(encoding="utf-8").strip()
    if not context:
        logging.info("Context file is empty, skipping")
        context_file.unlink(missing_ok=True)
        return

    logging.info("Flushing session %s: %d chars", session_id, len(context))

    # Run the LLM extraction
    response = asyncio.run(run_flush(context))

    # Classify on the MARKER, not on a substring found anywhere in the text, and test the
    # ERROR marker first. Both halves of the original ordering were wrong:
    #   - `"FLUSH_OK" in response` also matched an SDK error that echoed the prompt, and
    #     the prompt contains the literal `respond with exactly: FLUSH_OK`. A failure then
    #     took the SUCCESS branch: logged as "nothing worth saving", session recorded,
    #     context file deleted - exactly the loss WI-bugfix-4d7e21 exists to stop.
    #   - `"FLUSH_ERROR" in response` also matched a GENUINE summary that merely mentions
    #     the token, which is near-certain in this repo, whose sessions discuss this very
    #     pipeline. Real knowledge was then thrown away as if it were an error.
    # run_flush() mints both markers as a prefix, so anchor on that. Anything else counts
    # as content and gets SAVED, which is the fail-safe direction.
    if response.startswith("FLUSH_ERROR:"):
        # A failed flush PRESERVES its input instead of consuming it (WI-bugfix-4d7e21).
        # This branch used to append the error TEXT to the daily log - where compile.py
        # then read it as knowledge - and then fall through to record the session as
        # flushed and unlink the context file. A transient SDK failure therefore became
        # permanent silent loss: 1525 error stanzas across 142 daily logs, 12 days whose
        # log held nothing else, and a 22-session backfill on 2026-09-25 deleted outright.
        #
        # Returning leaves the capture on disk and the session unrecorded, and skips
        # maybe_trigger_compilation(), which is right - nothing new was compiled.
        # NOTE, deliberately not overstated: nothing re-reads a preserved file today. All
        # three producers mint a NEW timestamped context file, so there is no retry
        # consumer and these are manual-recovery material (WI-bugfix-1b9f04).
        # Exits NON-ZERO so backfill.py and run_flush.bat cannot report a false green.
        logging.error("Result: %s", response)
        logging.error("PRESERVED, not recorded and nothing appended: %s", context_file)
        return 1
    if response.strip() == "FLUSH_OK":
        logging.info("Result: FLUSH_OK")
        append_to_daily_log(
            "FLUSH_OK - Nothing worth saving from this session", "Memory Flush",
            target_date=target_date,
        )
    else:
        logging.info("Result: saved to daily log (%d chars)", len(response))
        append_to_daily_log(response, "Session", target_date=target_date)

    # Update dedup state — keep a per-session timestamp map so different
    # concurrent sessions don't shadow each other.
    sessions[session_id] = time.time()
    # Cap state size: keep only the 50 most recent sessions
    if len(sessions) > 50:
        sessions = dict(sorted(sessions.items(), key=lambda kv: kv[1], reverse=True)[:50])
    save_flush_state({"sessions": sessions, "last_session_id": session_id})

    # Clean up context file
    context_file.unlink(missing_ok=True)

    # No compile trigger here. It used to spawn compile.py after every flush past
    # 18:00, and since each flush changes today's log hash, a busy evening ran
    # 30-50 full Sonnet compiles (85 on 2026-10-04/05). The nightly
    # BrainOS-MemoryCompile task compiles once a day instead.

    logging.info("Flush complete for session %s", session_id)
    return 0


def _acquire_lock() -> bool:
    """Write the recursion-guard lock with this process's PID + start time.

    Returns False if the lock could NOT be established. The caller must then refuse to
    flush: this guard is what stops the Agent SDK's own child session from triggering
    another flush, and the 2026-05-07 runaway was 5,094 flushes over two days. A safety
    check that proceeds when it cannot be established is a check that fails OPEN, so the
    answer to "I could not write the lock" is "then do not run", not "run anyway".
    """
    try:
        FLUSH_LOCK.write_text(
            json.dumps({"pid": os.getpid(), "started": time.time()}),
            encoding="utf-8",
        )
        return True
    except OSError as e:
        logging.error("Could not write flush.lock, refusing to flush: %s", e)
        return False


def _release_lock() -> None:
    """Release the lock ONLY if this process owns it.

    An unconditional unlink strips the guard off a DIFFERENT flush that is still inside
    its SDK call: two near-simultaneous flushes, the second overwrites the lock with its
    own PID, the first finishes and deletes it, and the second now runs unguarded - the
    runaway path again. Whoever owns the lock releases it; a crashed owner is covered by
    session-end.py's 600-second staleness window.
    """
    try:
        if not FLUSH_LOCK.exists():
            return
        owner = json.loads(FLUSH_LOCK.read_text(encoding="utf-8")).get("pid")
        if owner != os.getpid():
            logging.info("Not releasing flush.lock: owned by pid %s, not %s", owner, os.getpid())
            return
        FLUSH_LOCK.unlink(missing_ok=True)
    except (OSError, ValueError) as e:
        logging.warning("Could not remove flush.lock: %s", e)


if __name__ == "__main__":
    # Hold the recursion-guard lock for the WHOLE flush, including the Agent
    # SDK call. session-end.py refuses to spawn while this lock is fresh, so
    # the child Claude Code session the SDK starts cannot trigger another
    # flush. Always released — even on crash — via try/finally.
    if not _acquire_lock():
        raise SystemExit(2)          # fail CLOSED - see _acquire_lock
    try:
        # SystemExit still runs the finally below, so the lock is released either way.
        raise SystemExit(main() or 0)
    finally:
        _release_lock()
