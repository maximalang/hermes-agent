"""PM gateway must not re-activate an obsolete in-tree venv after bootstrap."""

import sys


def test_windows_gateway_keeps_committed_pm_imports(monkeypatch, tmp_path):
    from gateway import run
    from pm import environments

    legacy = tmp_path / "venv" / "Lib" / "site-packages"
    legacy.mkdir(parents=True)
    monkeypatch.setattr(run, "__file__", str(tmp_path / "gateway" / "run.py"))
    monkeypatch.setattr(run.sys, "platform", "win32")
    monkeypatch.setattr(environments, "running_from_selected_environment", lambda root: True)
    monkeypatch.setenv("VIRTUAL_ENV", str(legacy.parent.parent))
    before = sys.path[:]
    try:
        run._ensure_windows_gateway_venv_imports()
        assert str(legacy) not in sys.path
    finally:
        sys.path[:] = before
