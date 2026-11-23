"""cli_model keeps its fallback and logs one warning when the shared settings cannot be read (WI-bugfix-8f9401)."""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import config  # noqa: E402


def test_cli_model_logs_a_warning_and_keeps_the_default(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("BRAINOS_DEV_ROOT", str(tmp_path))  # no shared/ here: the import fails
    monkeypatch.delitem(sys.modules, "shared.config.llm_config", raising=False)
    monkeypatch.delitem(sys.modules, "shared.config", raising=False)
    monkeypatch.delitem(sys.modules, "shared", raising=False)
    monkeypatch.setattr(sys, "path", [p for p in sys.path if "claude-code" + chr(92) + "dev" not in p and p != str(tmp_path)])
    with caplog.at_level(logging.WARNING):
        assert config.cli_model("memory_lint", "sonnet") == "sonnet"
    assert "memory_lint" in caplog.text
