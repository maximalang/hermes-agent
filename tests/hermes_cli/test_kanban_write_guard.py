"""#69283: kanban write guard prevents tests from writing to real ~/.hermes."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from hermes_cli import kanban_db
from hermes_cli import kanban_db_connect as kbc

# These probe the kanban guard against the real root on purpose.
pytestmark = pytest.mark.allow_real_home_io


def test_connect_succeeds_under_test_home(tmp_path, monkeypatch):
    """When HERMES_HOME is a temp dir, kanban connect succeeds normally."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    conn = kbc.connect()
    try:
        assert str(kanban_db.kanban_db_path()).startswith(str(home))
    finally:
        conn.close()


def test_connect_raises_when_kanban_home_is_real_root(monkeypatch):
    """When kanban paths resolve to the REAL root, connect raises RuntimeError."""
    import tests.conftest as _conftest

    monkeypatch.setattr(
        kanban_db, "kanban_home", lambda: _conftest._REAL_KANBAN_ROOT
    )
    monkeypatch.setattr(
        kanban_db,
        "kanban_db_path",
        lambda board=None: _conftest._REAL_KANBAN_ROOT / "kanban.db",
    )
    with pytest.raises(RuntimeError, match="kanban_write_guard"):
        kbc.connect()


def test_connect_raises_for_explicit_db_path_under_real_root():
    """Explicit db_path pointing under the real root is also refused."""
    import tests.conftest as _conftest

    with pytest.raises(RuntimeError, match="kanban_write_guard"):
        kbc.connect(_conftest._REAL_KANBAN_ROOT / "kanban.db")


def test_production_root_refused_even_when_fixture_captured_a_different_root(
    tmp_path, monkeypatch,
):
    """Windows/child-process native root cannot be opened by test code."""
    import hermes_state_guard

    pretend_production = tmp_path / "pretend-production"
    monkeypatch.setattr(
        hermes_state_guard, "_real_platform_state_root", lambda: pretend_production,
    )
    # A different captured root makes the old per-test wrapper miss this DB.
    # The fake production root is inside tmp_path; no live board is touched.
    path = pretend_production / "kanban" / "boards" / "alt" / "kanban.db"
    with pytest.raises(RuntimeError, match="kanban.*test.*isolation"):
        kbc.connect(path)
    assert not path.exists()
    assert not path.parent.exists()


def test_captured_native_root_survives_localappdata_override(tmp_path, monkeypatch):
    """Rehoming LOCALAPPDATA inside a test cannot disarm the import-time root."""
    import hermes_state_guard

    captured = tmp_path / "captured-root"
    monkeypatch.setenv("HERMES_TEST_REAL_KANBAN_ROOT", str(captured))
    monkeypatch.setattr(
        hermes_state_guard, "_real_platform_state_root", lambda: tmp_path / "other",
    )
    path = captured / "kanban.db"
    with pytest.raises(RuntimeError, match="kanban test isolation"):
        kbc.init_db(path)
    assert not path.exists()
    assert not path.parent.exists()


def test_subprocess_marker_refuses_fake_production_board(tmp_path):
    """A child without pytest fixtures must still reject a native-root DB."""
    fake_local = tmp_path / "fake-localappdata"
    db = fake_local / "hermes" / "kanban" / "boards" / "alt" / "kanban.db"
    source = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env.update({
        "LOCALAPPDATA": str(fake_local),
        "HERMES_HOME": str(tmp_path / "child-home"),
        "HERMES_KANBAN_DB": str(db),
        "HERMES_TEST_ISOLATION": "1",
        "PYTHONPATH": str(source),
    })
    for name in ("PYTEST_CURRENT_TEST", "PYTEST_VERSION"):
        env.pop(name, None)
    completed = subprocess.run(
        [sys.executable, "-c",
         "from hermes_cli import kanban_db_connect as kbc; kbc.connect()"],
        env=env, cwd=tmp_path, capture_output=True, text=True, timeout=20,
    )
    assert completed.returncode != 0
    assert "kanban test isolation" in completed.stderr
    assert not db.parent.exists()
