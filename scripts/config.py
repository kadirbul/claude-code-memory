"""Path constants and configuration for the personal knowledge base."""

from pathlib import Path
from datetime import datetime, timezone

# ── Paths ──────────────────────────────────────────────────────────────
ROOT_DIR = Path(__file__).resolve().parent.parent
DAILY_DIR = ROOT_DIR / "daily"
KNOWLEDGE_DIR = ROOT_DIR / "knowledge"
CONCEPTS_DIR = KNOWLEDGE_DIR / "concepts"
CONNECTIONS_DIR = KNOWLEDGE_DIR / "connections"
QA_DIR = KNOWLEDGE_DIR / "qa"
PROJECTS_DIR = KNOWLEDGE_DIR / "projects"
REPORTS_DIR = ROOT_DIR / "reports"
SCRIPTS_DIR = ROOT_DIR / "scripts"
HOOKS_DIR = ROOT_DIR / "hooks"
AGENTS_FILE = ROOT_DIR / "AGENTS.md"

INDEX_FILE = KNOWLEDGE_DIR / "index.md"
LOG_FILE = KNOWLEDGE_DIR / "log.md"
STATE_FILE = SCRIPTS_DIR / "state.json"

# ── Timezone ───────────────────────────────────────────────────────────
TIMEZONE = "America/Chicago"


def now_iso() -> str:
    """Current time in ISO 8601 format."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def today_iso() -> str:
    """Current date in ISO 8601 format."""
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")


def cli_model(task: str, default: str = "sonnet") -> str:
    """The model for one background Claude call (K789): the Brain OS model settings page's
    choice for `task`, else `default`. Works without Brain OS: if dev/shared is not there
    (BRAINOS_DEV_ROOT, else ../dev beside this repo), the default is used."""
    import os
    import sys
    dev_root = Path(os.getenv("BRAINOS_DEV_ROOT") or ROOT_DIR.parent / "dev")
    try:
        if str(dev_root) not in sys.path:
            sys.path.append(str(dev_root))
        from shared.config.llm_config import get_cli_model
        return get_cli_model(task, default)
    except Exception as exc:  # the fallback stays; the reason is logged once per call
        import logging
        logging.getLogger(__name__).warning("cli_model(%s): model settings unreadable (%s); using %s", task, exc, default)
        return default
