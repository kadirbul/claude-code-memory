"""
Backfill daily logs from existing Claude Code transcripts.

Use this when the SessionEnd/Stop hook missed sessions (e.g. because the
hook was misconfigured or the vault wasn't deployed yet). Reads the JSONL
transcripts under ~/.claude/projects/<project-key>/, extracts context using
the same logic as session-end.py, and invokes flush.py synchronously for
each so the daily/ logs and knowledge graph get populated.

Usage:
    uv run python scripts/backfill.py 2026-04-09 2026-04-10
    uv run python scripts/backfill.py --since 2026-04-09
    uv run python scripts/backfill.py --session 91ae6658-9675-4b39-9aa6-19172b3dccff

Notes:
    - Each session is run through flush.py which calls the Agent SDK once.
      Backfilling N sessions = N LLM calls. Plan accordingly.
    - The dedup window in flush.py is bypassed by clearing the per-session
      timestamp before each invocation (otherwise re-runs would no-op).
    - Sets CLAUDE_INVOKED_BY=memory_backfill so the Stop hook in any nested
      Agent SDK call exits cleanly (same recursion guard as flush.py).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
DAILY_DIR = ROOT / "daily"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
STATE_FILE = SCRIPTS_DIR / "last-flush.json"

# Resolve uv.exe by checking the well-known install location first, then PATH.
# subprocess.Popen on Windows does NOT search PATH for unqualified names the
# same way the shell does, so we always use a fully-qualified path.
def _resolve_uv() -> str:
    candidates = [
        Path.home() / ".local" / "bin" / "uv.exe",
        Path("C:/Users") / os.environ.get("USERNAME", "") / ".local" / "bin" / "uv.exe",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    # Fall back to PATH lookup via shutil.which
    import shutil
    found = shutil.which("uv") or shutil.which("uv.exe")
    if found:
        return found
    raise RuntimeError("uv.exe not found — install uv or update _resolve_uv()")

UV_EXE = _resolve_uv()

# Backfill uses much larger windows than the live hook because we want to
# capture as much of a marathon session as possible. Sessions that span
# multiple dates are chunked per-date so each date's daily log gets only
# the turns that actually happened that day.
MAX_TURNS_PER_CHUNK = 200
MAX_CHARS_PER_CHUNK = 100_000


def _entry_text(entry: dict) -> tuple[str, str]:
    """Return (role, text) for a JSONL entry, or ('','') if not a turn."""
    msg = entry.get("message", {})
    if isinstance(msg, dict):
        role = msg.get("role", "")
        content = msg.get("content", "")
    else:
        role = entry.get("role", "")
        content = entry.get("content", "")

    if role not in ("user", "assistant"):
        return "", ""

    if isinstance(content, list):
        text_parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif isinstance(block, str):
                text_parts.append(block)
        content = "\n".join(text_parts)

    if not (isinstance(content, str) and content.strip()):
        return "", ""

    return role, content.strip()


def _entry_local_date(entry: dict) -> str | None:
    """Return the local-tz YYYY-MM-DD of an entry's timestamp, or None."""
    ts = entry.get("timestamp")
    if not ts or not isinstance(ts, str):
        return None
    try:
        # CC writes UTC ISO timestamps with trailing Z
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def extract_chunks_by_date(transcript_path: Path) -> dict[str, tuple[str, int, str]]:
    """Group turns by entry-timestamp date and produce one context chunk per date.

    Returns: {date: (context_md, turn_count, first_user_text)}.
    Each chunk is independently capped at MAX_TURNS_PER_CHUNK / MAX_CHARS_PER_CHUNK
    to fit comfortably in the Agent SDK call.
    """
    by_date: dict[str, list[str]] = {}
    first_user_by_date: dict[str, str] = {}
    fallback_date: str | None = None

    with open(transcript_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            role, text = _entry_text(entry)
            if not role:
                continue

            date = _entry_local_date(entry)
            if not date:
                # If timestamp is missing, attribute to the most recent
                # known date so the turn isn't dropped.
                date = fallback_date or transcript_mtime_date(transcript_path)
            fallback_date = date

            label = "User" if role == "user" else "Assistant"
            by_date.setdefault(date, []).append(f"**{label}:** {text}\n")
            if role == "user" and date not in first_user_by_date:
                first_user_by_date[date] = text[:120]

    chunks: dict[str, tuple[str, int, str]] = {}
    for date, turns in by_date.items():
        # Keep the LAST N turns of each date — that's the most recent context
        # for that day, which is what summarization needs most.
        recent = turns[-MAX_TURNS_PER_CHUNK:]
        context = "\n".join(recent)
        if len(context) > MAX_CHARS_PER_CHUNK:
            context = context[-MAX_CHARS_PER_CHUNK:]
            boundary = context.find("\n**")
            if boundary > 0:
                context = context[boundary + 1:]
        chunks[date] = (context, len(recent), first_user_by_date.get(date, ""))

    return chunks


def transcript_mtime_date(p: Path) -> str:
    """Local-tz YYYY-MM-DD of the transcript's last-modified time."""
    mt = datetime.fromtimestamp(p.stat().st_mtime).astimezone()
    return mt.strftime("%Y-%m-%d")


def is_machine_session(p: Path) -> bool:
    """True for headless Agent SDK / `claude -p` runs (entrypoint "sdk-cli",
    or "sdk-py" for the Python Agent SDK this memory system itself uses).

    Those are product LLM calls and this memory system's own flush/compile
    runs - flushing them costs an LLM call each and captures nothing a human
    said. Interactive sessions carry "cli" / "claude-vscode" instead.
    """
    # Product LLM calls via shared/lib/llm.py run `claude -p` from this temp
    # cwd and inherit the parent's entrypoint ("claude-vscode"), so the folder
    # is the only reliable marker for them.
    if "claude-code-isolated" in p.parent.name:
        return True
    try:
        with p.open(encoding="utf-8", errors="ignore") as fh:
            for i, line in enumerate(fh):
                if i >= 20:
                    break
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "entrypoint" in entry:
                    return str(entry["entrypoint"]).startswith("sdk-")
    except OSError:
        pass
    return False


def find_transcripts(
    dates: set[str],
    session_filter: str | None,
    skip_session_ids: set[str],
) -> list[Path]:
    """Find all top-level .jsonl transcripts whose mtime falls on one of the given dates.

    Excludes any session_id in `skip_session_ids` (e.g. the live session and
    any session the user has open in another tab). Subagent transcripts under
    `<session>/subagents/*.jsonl` are NOT included — they're sub-conversations
    spawned by the parent session, and the parent's transcript already
    contains references to them.
    """
    if not PROJECTS_DIR.exists():
        return []

    matches: list[Path] = []
    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl in project_dir.glob("*.jsonl"):
            if jsonl.stem in skip_session_ids:
                continue
            if session_filter and session_filter not in jsonl.stem:
                continue
            # When --session is given, the user is being explicit — don't
            # also filter by mtime (a long session may have started in-scope
            # but its file mtime is today). When --session is NOT given, the
            # mtime filter prevents pulling in unrelated old sessions.
            if session_filter:
                matches.append(jsonl)
                continue
            if not dates or transcript_mtime_date(jsonl) in dates:
                if not is_machine_session(jsonl):
                    matches.append(jsonl)
    return sorted(matches, key=lambda p: p.stat().st_mtime)


def clear_dedup_for(session_id: str) -> None:
    """Remove this session_id from the flush dedup state so flush.py runs."""
    if not STATE_FILE.exists():
        return
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    sessions = state.get("sessions", {})
    if isinstance(sessions, dict) and session_id in sessions:
        del sessions[session_id]
        state["sessions"] = sessions
        STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def run_flush_for_chunk(
    session_id: str,
    target_date: str,
    context: str,
    turn_count: int,
    title: str,
    source_jsonl: Path,
) -> bool:
    """Run flush.py for one (session, date) chunk. Returns True on success."""
    # Write context md, same naming convention session-end.py uses but with date
    timestamp = datetime.fromtimestamp(source_jsonl.stat().st_mtime).astimezone().strftime("%Y%m%d-%H%M%S")
    context_file = SCRIPTS_DIR / f"backfill-{session_id}-{target_date}-{timestamp}.md"
    header = (
        f"<!-- backfill: {source_jsonl.name} | date={target_date} | "
        f"turns={turn_count} | first_user={title!r} -->\n\n"
    )
    context_file.write_text(header + context, encoding="utf-8")

    # Each (session, date) chunk needs its own dedup-bypass
    clear_dedup_for(session_id)

    flush_script = SCRIPTS_DIR / "flush.py"
    cmd = [
        UV_EXE, "run", "--directory", str(ROOT), "python",
        str(flush_script), str(context_file), session_id, target_date,
    ]

    # Recursion guard: tell any nested Agent SDK Stop-hook to exit immediately
    env = dict(os.environ)
    env["CLAUDE_INVOKED_BY"] = "memory_backfill"

    creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

    print(
        f"  RUN  {session_id[:8]} [{target_date}]: {turn_count} turns, "
        f"{len(context):,} chars \u2014 {title[:60]}", flush=True,
    )
    t0 = time.time()
    try:
        result = subprocess.run(
            cmd, env=env, creationflags=creation_flags,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300,
        )
        dt = time.time() - t0
        if result.returncode != 0:
            print(f"  FAIL {session_id[:8]} [{target_date}]: rc={result.returncode} in {dt:.1f}s", flush=True)
            if result.stderr:
                print(f"       stderr: {result.stderr.decode('utf-8', errors='replace')[:300]}", flush=True)
            return False
        print(f"  OK   {session_id[:8]} [{target_date}]: flushed in {dt:.1f}s", flush=True)
        return True
    except subprocess.TimeoutExpired:
        print(f"  TIME {session_id[:8]} [{target_date}]: flush.py exceeded 300s", flush=True)
        return False
    except Exception as e:
        print(f"  ERR  {session_id[:8]} [{target_date}]: subprocess failed: {e}", flush=True)
        return False


def run_flush_for(jsonl: Path, only_dates: set[str] | None) -> tuple[int, int]:
    """Extract per-date chunks from a transcript and flush each one.

    only_dates — if non-empty, only flush chunks whose date is in this set.
    Returns (ok_count, fail_count).
    """
    session_id = jsonl.stem

    try:
        chunks = extract_chunks_by_date(jsonl)
    except Exception as e:
        print(f"  ERR  {session_id[:8]}: extract failed: {e}", flush=True)
        return (0, 1)

    if not chunks:
        print(f"  SKIP {session_id[:8]}: no turns extracted", flush=True)
        return (0, 0)

    ok = 0
    fail = 0
    for date in sorted(chunks.keys()):
        if only_dates and date not in only_dates:
            continue
        context, turn_count, title = chunks[date]
        if not context.strip() or turn_count == 0:
            continue
        if run_flush_for_chunk(session_id, date, context, turn_count, title, jsonl):
            ok += 1
        else:
            fail += 1
    return (ok, fail)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dates", nargs="*", help="Specific YYYY-MM-DD dates to backfill (matches both transcript mtime AND the date of each chunk)")
    parser.add_argument("--since", help="Backfill all transcripts mtime >= this date (YYYY-MM-DD)")
    parser.add_argument("--session", help="Backfill only this session id (substring match)")
    parser.add_argument("--skip", action="append", default=[], help="Session id to skip (repeatable, substring match against full id)")
    parser.add_argument("--dry-run", action="store_true", help="List matches and chunk breakdown without invoking flush.py")
    args = parser.parse_args()

    dates: set[str] = set(args.dates)
    if args.since:
        try:
            since = datetime.strptime(args.since, "%Y-%m-%d").date()
        except ValueError:
            print(f"Invalid --since date: {args.since}", file=sys.stderr)
            sys.exit(2)
        today = datetime.now().date()
        d = since
        while d <= today:
            dates.add(d.strftime("%Y-%m-%d"))
            d = d.fromordinal(d.toordinal() + 1)

    skip_session_ids: set[str] = set(args.skip)

    # find_transcripts uses substring match on stem; expand any partial skips
    # to full ids by scanning the directory once.
    if skip_session_ids and PROJECTS_DIR.exists():
        expanded: set[str] = set()
        for project_dir in PROJECTS_DIR.iterdir():
            if not project_dir.is_dir():
                continue
            for jsonl in project_dir.glob("*.jsonl"):
                for s in skip_session_ids:
                    if s in jsonl.stem:
                        expanded.add(jsonl.stem)
        skip_session_ids = expanded

    transcripts = find_transcripts(dates, args.session, skip_session_ids)
    if not transcripts:
        print("No transcripts matched the filter.", flush=True)
        return

    print(f"Found {len(transcripts)} transcript(s) for backfill:", flush=True)
    for p in transcripts:
        size_mb = p.stat().st_size / (1024 * 1024)
        print(f"  mtime={transcript_mtime_date(p)}  {p.parent.name}/{p.name}  ({size_mb:.2f} MB)", flush=True)
    if skip_session_ids:
        print(f"\nSkipping: {sorted(skip_session_ids)}", flush=True)

    if args.dry_run:
        print("\n[dry-run] Per-date chunk breakdown:", flush=True)
        for p in transcripts:
            try:
                chunks = extract_chunks_by_date(p)
            except Exception as e:
                print(f"  {p.stem[:8]}: extract failed: {e}", flush=True)
                continue
            for date in sorted(chunks):
                ctx, n_turns, title = chunks[date]
                in_scope = (not dates) or (date in dates)
                marker = "  " if in_scope else " ~"
                print(f"{marker}{p.stem[:8]} [{date}] {n_turns} turns, {len(ctx):,} chars  \u2014  {title[:60]}", flush=True)
        return

    print("\nRunning flush.py per (session, date) chunk...\n", flush=True)
    total_ok = 0
    total_fail = 0
    for p in transcripts:
        ok, fail = run_flush_for(p, only_dates=dates)
        total_ok += ok
        total_fail += fail

    print(f"\nDone. chunks_flushed={total_ok} chunks_failed={total_fail}", flush=True)
    print(f"Daily logs: {DAILY_DIR}", flush=True)


if __name__ == "__main__":
    main()
