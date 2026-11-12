"""Exercise CLI workflows in process; executable handoff has separate subprocess checks."""
import pytest

@pytest.fixture(autouse=True)
def in_process_cli(monkeypatch):
    monkeypatch.setattr("pace.runtime.pinned_runtime.reexec_pinned_runtime", lambda *args: None)
