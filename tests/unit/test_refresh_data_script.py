"""Unit tests for `scripts/refresh_data.py`'s `main()`.

Loaded directly by file path via `importlib` rather than a normal package
import -- `scripts/` is a standalone CLI entrypoint directory, deliberately
not an importable package (see `backend/graph/__init__.py`-style modules
for the contrast; CLAUDE.md's project structure lists `scripts/
refresh_data.py` as a lone file). `module.__name__` is set to something
other than `"__main__"` here, so the script's own `if __name__ ==
"__main__": sys.exit(main())` guard never fires on import -- `main()` is
called explicitly by each test instead.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "refresh_data.py"


@dataclass
class _FakeRefreshResult:
    status: str
    terminals_count: Optional[int] = None
    commodities_count: Optional[int] = None
    prices_count: Optional[int] = None
    distances_count: Optional[int] = None
    error_message: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.status == "success"


@pytest.fixture
def script_module():
    spec = importlib.util.spec_from_file_location("refresh_data_script_under_test", _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_main_returns_zero_and_logs_counts_on_success(script_module, monkeypatch, caplog):
    fake_result = _FakeRefreshResult(
        status="success", terminals_count=10, commodities_count=5, prices_count=40, distances_count=90
    )
    monkeypatch.setattr(script_module, "run_refresh", lambda settings: fake_result)

    with caplog.at_level("INFO"):
        exit_code = script_module.main()

    assert exit_code == 0
    assert any("succeeded" in message for message in caplog.messages)


def test_main_returns_one_and_logs_error_on_failure(script_module, monkeypatch, caplog):
    fake_result = _FakeRefreshResult(status="failed", error_message="external API unreachable")
    monkeypatch.setattr(script_module, "run_refresh", lambda settings: fake_result)

    with caplog.at_level("ERROR"):
        exit_code = script_module.main()

    assert exit_code == 1
    assert any("external API unreachable" in message for message in caplog.messages)
