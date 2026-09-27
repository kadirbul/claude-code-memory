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


COMPILE_AFTER_HOUR = 18  # 6 PM local time


def maybe_trigger_compilation() -> None:
    """If it's past the compile hour and today's log hasn't been compiled, run compile.py."""
    import subprocess as _sp

    now = datetime.now(timezone.utc).astimezone()
    if now.hour < COMPILE_AFTER_HOUR:
        return

    # Check if today's log has already been compiled
    today_log = f"{now.strftime('%Y-%m-%d')}.md"
    compile_state_file = SCRIPTS_DIR / "state.json"
    if compile_state_file.exists():
        try:
            compile_state = json.loads(compile_state_file.read_text(encoding="utf-8"))
            ingested = compile_state.get("ingested", {})
            if today_log in ingested:
                # Already compiled today - check if the log has changed since
                from hashlib import sha256
                log_path = DAILY_DIR / today_log
                if log_path.exists():
                    current_hash = sha256(log_path.read_bytes()).hexdigest()[:16]
                    if ingested[today_log].get("hash") == current_hash:
                        return  # log unchanged since last compile
        except (json.JSONDecodeError, OSError):
            pass

    compile_script = SCRIPTS_DIR / "compile.py"
    if not compile_script.exists():
        return

    logging.info("End-of-day compilation triggered (after %d:00)", COMPILE_AFTER_HOUR)

    # Resolve uv.exe with full path — Windows subprocess.Popen does not
    # search PATH the same way the shell does, so unqualified `uv` fails
    # with WinError 2 even when uv is on PATH for an interactive shell.
    import shutil as _shutil
    uv_exe = (
        _shutil.which("uv")
        or _shutil.which("uv.exe")
        or str(Path.home() / ".local" / "bin" / "uv.exe")
    )
    cmd = [uv_exe, "run", "--directory", str(ROOT), "python", str(compile_script)]

    kwargs: dict = {}
    if sys.platform == "win32":
        # Use CREATE_NO_WINDOW — DO NOT use DETACHED_PROCESS as it breaks
        # the Agent SDK's subprocess I/O (compile.py calls Claude Agent SDK).
        kwargs["creationflags"] = _sp.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True

    try:
        log_handle = open(str(SCRIPTS_DIR / "compile.log"), "a")
        _sp.Popen(cmd, stdout=log_handle, stderr=_sp.STDOUT, cwd=str(ROOT), **kwargs)
    except Exception as e:
        logging.error("Failed to spawn compile.py: %s", e)


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

    # Append to daily log
    if "FLUSH_OK" in response:
        logging.info("Result: FLUSH_OK")
        append_to_daily_log(
            "FLUSH_OK - Nothing worth saving from this session", "Memory Flush",
            target_date=target_date,
        )
    elif "FLUSH_ERROR" in response:
        # A failed flush PRESERVES its input instead of consuming it (WI-bugfix-4d7e21).
        # This branch used to append the error TEXT to the daily log - where compile.py
        # then read it as knowledge - and then fall through to record the session as
        # flushed and unlink the context file. A transient SDK failure therefore became
        # permanent silent loss: 1525 error stanzas across 142 daily logs, 12 days whose
        # log held nothing else, and a 22-session backfill on 2026-09-25 deleted outright.
        #
        # Returning here leaves the capture on disk AND the session unrecorded, so the
        # 30-minute dedup window does not apply and the next flush for this session
        # retries it. It also skips maybe_trigger_compilation(), which is correct: there
        # is nothing new to compile.
        logging.error("Result: %s", response)
        logging.error("PRESERVED for retry - not recorded, nothing appended: %s", context_file)
        return
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

    # End-of-day auto-compilation: if it's past the compile hour and today's
    # log hasn't been compiled yet, trigger compile.py in the background.
    maybe_trigger_compilation()

    logging.info("Flush complete for session %s", session_id)


def _acquire_lock() -> None:
    """Write the recursion-guard lock with this process's PID + start time."""
    try:
        FLUSH_LOCK.write_text(
            json.dumps({"pid": os.getpid(), "started": time.time()}),
            encoding="utf-8",
        )
    except OSError as e:
        logging.warning("Could not write flush.lock: %s", e)


def _release_lock() -> None:
    try:
        FLUSH_LOCK.unlink(missing_ok=True)
    except OSError as e:
        logging.warning("Could not remove flush.lock: %s", e)


if __name__ == "__main__":
    # Hold the recursion-guard lock for the WHOLE flush, including the Agent
    # SDK call. session-end.py refuses to spawn while this lock is fresh, so
    # the child Claude Code session the SDK starts cannot trigger another
    # flush. Always released — even on crash — via try/finally.
    _acquire_lock()
    try:
        main()
    finally:
        _release_lock()
