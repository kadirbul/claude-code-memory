"""
SessionEnd hook - captures conversation transcript for memory extraction.

When a Claude Code session ends, this hook reads the transcript path from
stdin, extracts conversation context, and spawns flush.py as a background
process to extract knowledge into the daily log.

The hook itself does NO API calls - only local file I/O for speed (<10s).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Recursion guard: if we were spawned by flush.py (which calls Agent SDK,
# which runs Claude Code, which would fire this hook again), exit immediately.
if os.environ.get("CLAUDE_INVOKED_BY"):
    sys.exit(0)

ROOT = Path(__file__).resolve().parent.parent
DAILY_DIR = ROOT / "daily"
SCRIPTS_DIR = ROOT / "scripts"
STATE_DIR = SCRIPTS_DIR

logging.basicConfig(
    filename=str(SCRIPTS_DIR / "flush.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [hook] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

MAX_TURNS = 30
MAX_CONTEXT_CHARS = 15_000
MIN_TURNS_TO_FLUSH = 1


def extract_conversation_context(transcript_path: Path) -> tuple[str, int]:
    """Read JSONL transcript and extract last ~N conversation turns as markdown."""
    turns: list[str] = []

    with open(transcript_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg = entry.get("message", {})
            if isinstance(msg, dict):
                role = msg.get("role", "")
                content = msg.get("content", "")
            else:
                role = entry.get("role", "")
                content = entry.get("content", "")

            if role not in ("user", "assistant"):
                continue

            if isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif isinstance(block, str):
                        text_parts.append(block)
                content = "\n".join(text_parts)

            if isinstance(content, str) and content.strip():
                label = "User" if role == "user" else "Assistant"
                turns.append(f"**{label}:** {content.strip()}\n")

    recent = turns[-MAX_TURNS:]
    context = "\n".join(recent)

    if len(context) > MAX_CONTEXT_CHARS:
        context = context[-MAX_CONTEXT_CHARS:]
        boundary = context.find("\n**")
        if boundary > 0:
            context = context[boundary + 1 :]

    return context, len(recent)


def main() -> None:
    # Read hook input from stdin
    # Claude Code on Windows may pass paths with unescaped backslashes
    raw_input = ""
    try:
        raw_input = sys.stdin.read()
        try:
            hook_input: dict = json.loads(raw_input)
        except json.JSONDecodeError:
            fixed_input = re.sub(r'(?<!\\)\\(?!["\\])', r'\\\\', raw_input)
            hook_input = json.loads(fixed_input)
    except (json.JSONDecodeError, ValueError, EOFError) as e:
        logging.error("Failed to parse stdin: %s | raw=%r", e, raw_input[:500])
        return

    session_id = hook_input.get("session_id", "")
    source = hook_input.get("source", "unknown")
    event_name = hook_input.get("hook_event_name", "unknown")
    transcript_path_str = hook_input.get("transcript_path", "")
    cwd = hook_input.get("cwd", "")

    logging.info(
        "Hook fired: event=%s session=%s source=%s cwd=%s xpath=%s",
        event_name, session_id or "<empty>", source,
        cwd or "<empty>", transcript_path_str or "<empty>",
    )

    # Fallback: if transcript_path is missing but we have a session_id and cwd,
    # reconstruct the path from the standard CC layout:
    #   ~/.claude/projects/<project-key>/<session_id>.jsonl
    # The project-key is the absolute cwd with separators replaced by '-' and
    # leading drive ':' stripped (e.g. 'C--claude-code-brain-os').
    if not transcript_path_str and session_id and cwd:
        try:
            home = Path.home()
            key = re.sub(r'[\\/:]+', '-', cwd).strip('-')
            candidate = home / ".claude" / "projects" / key / f"{session_id}.jsonl"
            if candidate.exists():
                transcript_path_str = str(candidate)
                logging.info("Recovered transcript_path via cwd+session_id: %s", candidate)
        except Exception as e:
            logging.warning("Transcript recovery failed: %s", e)

    if not transcript_path_str or not isinstance(transcript_path_str, str):
        # Log full payload (truncated) so we can see what CC actually sent.
        logging.info(
            "SKIP: no transcript path. payload_keys=%s raw=%r",
            list(hook_input.keys()), raw_input[:500],
        )
        return

    transcript_path = Path(transcript_path_str)
    if not transcript_path.exists():
        logging.info("SKIP: transcript missing: %s", transcript_path_str)
        return

    # Extract conversation context in the hook (fast, no API calls)
    try:
        context, turn_count = extract_conversation_context(transcript_path)
    except Exception as e:
        logging.error("Context extraction failed: %s", e)
        return

    if not context.strip():
        logging.info("SKIP: empty context")
        return

    if turn_count < MIN_TURNS_TO_FLUSH:
        logging.info("SKIP: only %d turns (min %d)", turn_count, MIN_TURNS_TO_FLUSH)
        return

    # Write context to a temp file for the background process
    timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
    context_file = STATE_DIR / f"session-flush-{session_id}-{timestamp}.md"
    context_file.write_text(context, encoding="utf-8")

    # Spawn flush.py as a background process
    flush_script = SCRIPTS_DIR / "flush.py"

    # Use full path to uv.exe — when hooks fire from /usr/bin/bash on Windows,
    # "uv" alone isn't on PATH (causes WinError 2 / "command not found").
    import shutil as _shutil
    uv_exe = (
        _shutil.which("uv")
        or _shutil.which("uv.exe")
        or str(Path.home() / ".local" / "bin" / "uv.exe")
    )

    cmd = [
        uv_exe,
        "run",
        "--directory",
        str(ROOT),
        "python",
        str(flush_script),
        str(context_file),
        session_id,
    ]

    # On Windows, use CREATE_NO_WINDOW to avoid flash console window.
    # Do NOT use DETACHED_PROCESS — it breaks the Agent SDK's subprocess I/O.
    creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
        logging.info("Spawned flush.py for session %s (%d turns, %d chars)", session_id, turn_count, len(context))
    except Exception as e:
        logging.error("Failed to spawn flush.py: %s", e)


if __name__ == "__main__":
    main()
