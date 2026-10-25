"""``kanban gc`` must actually remove archived scratch workspaces that contain
read-only files (git object stores from repo clones) and must report, per
workspace, whatever still refuses to go — instead of silently swallowing the
failure and miscounting (GC follow-up: 184 of 259 archived tasks kept ~6.6 GB
on disk after a "successful" sweep)."""
import argparse
import contextlib
import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_ops


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    return tmp_path


def _args():
    # Retention sweeps disabled: this file is about the workspace lane only.
    return argparse.Namespace(event_retention_days=0, log_retention_days=0)


def _archived_ws(conn, title: str):
    tid = kb.create_task(conn, title=title)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status='archived', workspace_kind='scratch' WHERE id=?",
            (tid,),
        )
    ws = kb.workspaces_root() / tid
    ws.mkdir(parents=True)
    return tid, ws


def _chmod_tree_writable(root: Path) -> None:
    """Teardown guard: a regressed sweep may leave read-only entries behind;
    pytest's tmp cleanup would trip over them on Windows."""
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            with contextlib.suppress(OSError):
                os.chmod(os.path.join(dirpath, name), 0o755)
    with contextlib.suppress(OSError):
        os.chmod(root, 0o755)


def test_gc_removes_readonly_git_workspace_and_counts_it(board, capsys):
    """A .git-like tree with a read-only loose object inside a read-only
    directory must be fully removed and counted exactly once. Pre-fix
    ``rmtree(ignore_errors=True)`` leaves it behind on Windows (read-only
    attribute) and on POSIX (0o555 parent blocks unlink) — silently."""
    with kbc.connect_closing() as conn:
        tid, ws = _archived_ws(conn, "archived with git clone")
    objects = ws / ".git" / "objects"
    (objects / "pack").mkdir(parents=True)
    loose = objects / "ab"
    loose.mkdir(parents=True)
    obj = loose / "cdef1234567890"
    obj.write_bytes(b"\x78\x01fake loose object")
    (objects / "pack" / "pack-abc.pack").write_bytes(b"fake pack")
    (ws / "notes.txt").write_text("scratch")
    os.chmod(obj, 0o444)
    os.chmod(loose, 0o555)
    try:
        assert kanban_ops._cmd_gc(_args()) == 0
        assert not ws.exists(), f"read-only workspace survived gc: {ws}"
        out = capsys.readouterr().out
        assert "1 workspace(s)" in out
        assert "failed" not in out
    finally:
        _chmod_tree_writable(ws)


def test_gc_reports_and_does_not_count_unremovable_workspace(board, monkeypatch, capsys):
    """When a workspace cannot be removed even with the chmod-retry (open
    handle, ACL, ...), gc must keep exit code 0 for the healthy lane, count
    only actual deletions, and log the survivor with id, path and errno."""
    with kbc.connect_closing() as conn:
        tid_ok, ws_ok = _archived_ws(conn, "archived healthy")
        tid_bad, ws_bad = _archived_ws(conn, "archived stuck")
    (ws_ok / "f.txt").write_text("x")
    (ws_bad / "f.txt").write_text("x")

    real_rmtree = kanban_ops.rmtree_readonly  # pre-fix: AttributeError -> RED

    def stuck(path, **kwargs):
        if Path(path) == ws_bad:
            raise PermissionError(13, "Permission denied")
        return real_rmtree(path, **kwargs)

    monkeypatch.setattr(kanban_ops, "rmtree_readonly", stuck)
    assert kanban_ops._cmd_gc(_args()) == 0
    assert not ws_ok.exists()
    assert ws_bad.exists()
    captured = capsys.readouterr()
    assert "1 workspace(s)" in captured.out
    assert "1 workspace(s) failed" in captured.out
    assert tid_bad in captured.err
    assert str(ws_bad) in captured.err
    assert "Errno 13" in captured.err
