"""Worker bootstrap and unknown-profile routing must fail closed."""
from __future__ import annotations

from pathlib import Path

import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

# The activated interpreter and the scoped worktree live below the Windows
# Hermes home; allowing their source/runtime reads does not permit live DB writes.
pytestmark = pytest.mark.allow_real_home_io


def test_worker_uses_interpreter_from_its_own_install(monkeypatch, tmp_path):
    root = tmp_path / "installed-hermes"
    package = root / "hermes_cli"
    package.mkdir(parents=True)
    # Materialize both interpreter layouts so the resolver's host branch
    # finds the checkout's venv on Windows and on POSIX CI lanes alike.
    windows_interpreter = root / "venv" / "Scripts" / "python.exe"
    posix_interpreter = root / "venv" / "bin" / "python"
    for candidate in (windows_interpreter, posix_interpreter):
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.touch()
    expected = windows_interpreter if kb._IS_WINDOWS else posix_interpreter
    with monkeypatch.context() as patcher:
        patcher.setattr(kbd, "__file__", str(package / "kanban_db_dispatch.py"))
        patcher.delenv("HERMES_BIN", raising=False)
        argv = kbd._resolve_hermes_argv()
    assert argv == [str(expected), "-m", "hermes_cli.main"]


def test_unavailable_profile_registry_never_claims_or_spawns(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "sandbox"))
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    spawned = []
    monkeypatch.setattr(kbd, "_default_spawn", lambda *args, **kwargs: spawned.append(args))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="unknown worker", assignee="missing-profile")
        result = kbd.dispatch_once(conn)
        task = kb.get_task(conn, tid)
        runs = kb.list_runs(conn, tid)
    assert not spawned
    assert result.skipped_nonspawnable == [tid]
    assert task.status == "ready" and task.claim_lock is None
    assert task.consecutive_failures == 0 and not runs
