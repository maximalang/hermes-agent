"""Invariant: an open ``task_runs`` row that no live worker can still own is
closed by the canonical orphaned-run reaper (incident t_9090afcd, run 2512).

``reap_orphaned_runs`` closes a row (``ended_at`` set, outcome ``orphaned``)
only when its task is archived/done/missing, or its host-local owner pid is
dead, or it exceeds the documented max age with an unverifiable owner. A live
worker is never reaped, an already-ended row is never touched, and repeated
sweeps are idempotent.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv(kbd.ORPHAN_RUN_MAX_AGE_ENV, raising=False)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    with kbc.connect() as c:
        yield c


def _dead_pid() -> int:
    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    proc.wait(timeout=30)
    return proc.pid


def _open_run(conn, *, status_after: str, pid: int, claim_lock: str, started_ago: int = 0):
    """Create a task with one open run owned by ``pid``; returns (task_id, run_id)."""
    tid = kb.create_task(conn, title="orphan candidate", assignee="coder")
    kb.claim_task(conn, tid, claimer=claim_lock)
    run_id = kb._current_run_id(conn, tid)
    conn.execute("UPDATE task_runs SET worker_pid = ?, claim_lock = ?, started_at = ? WHERE id = ?",
                 (pid, claim_lock, int(time.time()) - started_ago, run_id))
    conn.execute("UPDATE tasks SET worker_pid = ?, current_run_id = ? WHERE id = ?", (pid, run_id, tid))
    if status_after != "running":
        conn.execute("UPDATE tasks SET status = ?, current_run_id = NULL WHERE id = ?", (status_after, tid))
    conn.commit()
    return tid, run_id


def _run(conn, run_id):
    return conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()


def test_dead_pid_on_archived_task_is_reaped(conn):
    host = kb._host_prefix()
    tid, run_id = _open_run(conn, status_after="archived", pid=_dead_pid(), claim_lock=f"{host}x")
    reaped = kbd.reap_orphaned_runs(conn)
    assert reaped == [run_id]
    row = _run(conn, run_id)
    assert row["ended_at"] is not None
    assert row["outcome"] == "orphaned" and row["status"] == "orphaned"
    assert "task_terminal:archived" in row["error"]
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=?", (tid,))]
    assert "orphan_run_reaped" in kinds


def test_dead_pid_on_done_task_is_reaped_even_without_fingerprint(conn):
    # Legacy row: worker_started_at NULL, matching run 2512's shape.
    host = kb._host_prefix()
    _, run_id = _open_run(conn, status_after="done", pid=_dead_pid(), claim_lock=f"{host}x")
    assert kbd.reap_orphaned_runs(conn) == [run_id]
    assert _run(conn, run_id)["ended_at"] is not None


def test_live_worker_on_archived_task_is_preserved(conn):
    host = kb._host_prefix()
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(0.2)
        _, run_id = _open_run(conn, status_after="archived", pid=sleeper.pid, claim_lock=f"{host}x")
        assert kbd.reap_orphaned_runs(conn) == []
        assert _run(conn, run_id)["ended_at"] is None
    finally:
        sleeper.kill()
        sleeper.wait()


def test_running_current_run_is_left_to_reclaim_sweeps(conn):
    host = kb._host_prefix()
    _, run_id = _open_run(conn, status_after="running", pid=_dead_pid(), claim_lock=f"{host}x")
    assert kbd.reap_orphaned_runs(conn) == []
    assert _run(conn, run_id)["ended_at"] is None


def test_already_ended_row_is_unchanged(conn):
    host = kb._host_prefix()
    tid, run_id = _open_run(conn, status_after="archived", pid=_dead_pid(), claim_lock=f"{host}x")
    conn.execute("UPDATE task_runs SET ended_at = 111, outcome = 'completed', status = 'done' WHERE id = ?",
                 (run_id,))
    conn.commit()
    before = dict(_run(conn, run_id))
    assert kbd.reap_orphaned_runs(conn) == []
    assert dict(_run(conn, run_id)) == before


def test_repeated_sweep_is_idempotent(conn):
    host = kb._host_prefix()
    _, run_id = _open_run(conn, status_after="archived", pid=_dead_pid(), claim_lock=f"{host}x")
    assert kbd.reap_orphaned_runs(conn) == [run_id]
    snapshot = dict(_run(conn, run_id))
    assert kbd.reap_orphaned_runs(conn) == []
    assert kbd.reap_orphaned_runs(conn) == []
    assert dict(_run(conn, run_id)) == snapshot


def test_remote_young_run_is_preserved_but_max_age_reaps(conn, monkeypatch):
    monkeypatch.setenv(kbd.ORPHAN_RUN_MAX_AGE_ENV, "3600")
    _, young = _open_run(conn, status_after="running", pid=None, claim_lock="otherhost:1", started_ago=60)
    # Make the task non-current so only the max-age rule can apply.
    conn.execute("UPDATE tasks SET status='ready', current_run_id=NULL WHERE current_run_id=?", (young,))
    conn.commit()
    assert kbd.reap_orphaned_runs(conn) == []
    conn.execute("UPDATE task_runs SET started_at = started_at - 7200 WHERE id = ?", (young,))
    conn.commit()
    assert kbd.reap_orphaned_runs(conn) == [young]
    assert "max_run_age" in _run(conn, young)["error"]


def test_dispatch_tick_invokes_orphan_reaper(conn):
    host = kb._host_prefix()
    _, run_id = _open_run(conn, status_after="archived", pid=_dead_pid(), claim_lock=f"{host}x")
    result = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 0, dry_run=True, max_spawn=0)
    assert result.orphan_runs_reaped == [run_id]
    assert _run(conn, run_id)["ended_at"] is not None
