"""Reclaim must also terminate the run's bound interpreter, not only the dispatcher's submitter.

2026-10-21 fleet-ops incident: TTL reclaim killed the spawned submitter while the
gateway-managed worker (the ``worker_bound`` interpreter) stayed alive as a ~200 MB zombie,
because it had wedged in startup before its own submitter-watch began. The signal keys on the
run's ``worker_bound`` pid + fingerprint: a recycled or unverified pid is never signalled."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _claimed_task(conn):
    tid = kb.create_task(conn, title="bound interpreter reclaim", assignee="worker")
    assert kb.claim_task(conn, tid) is not None
    return tid, kb.latest_run(conn, tid)


def test_ttl_reclaim_signals_bound_interpreter(kanban_home, monkeypatch):
    conn = kbc.connect()
    signalled: list[tuple[int, int]] = []
    try:
        tid, run1 = _claimed_task(conn)
        kbd._set_worker_pid(conn, tid, 4242)  # the dispatcher's submitter: already gone
        lock = kb.get_task(conn, tid).claim_lock
        kb._append_event(conn, tid, "worker_bound",
                         {"pid": 9191, "claim_lock": lock, "started_at": "2026-10-21T09:00:00.000000"},
                         run_id=run1.id)
        # 4242 dead (reclaim proceeds), 9191 alive and matching its recorded fingerprint.
        monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid == 9191)
        monkeypatch.setattr(kbd, "_pid_alive", lambda pid: pid == 9191)
        monkeypatch.setattr(kbd, "_pid_recycled", lambda pid, started: False)
        monkeypatch.setattr(kbd, "_poll_worker_exit", lambda pid, started: True)
        with kb.write_txn(conn):
            aged = int(time.time()) - 30
            conn.execute("UPDATE tasks SET started_at = ? WHERE id = ?", (aged, tid))
            conn.execute("UPDATE task_runs SET started_at = ? WHERE id = ?", (aged, run1.id))
        assert kbd.detect_stale_running(
            conn, stale_timeout_seconds=1,
            signal_fn=lambda pid, sig: signalled.append((pid, int(sig)))) == [tid]
        assert 9191 in [pid for pid, _ in signalled]  # the bound interpreter was signalled
        task = kb.get_task(conn, tid)
        assert task.status == "ready" and task.claim_lock is None
    finally:
        conn.close()


def test_operator_reclaim_never_signals_recycled_bound_pid(kanban_home, monkeypatch):
    conn = kbc.connect()
    signalled: list[tuple[int, int]] = []
    try:
        tid, run1 = _claimed_task(conn)
        kbd._set_worker_pid(conn, tid, 4242)
        lock = kb.get_task(conn, tid).claim_lock
        monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid == 9191)
        monkeypatch.setattr(kbd, "_pid_alive", lambda pid: pid == 9191)
        # pid 9191 is alive but its creation time no longer matches the recorded fingerprint.
        monkeypatch.setattr(kbd, "_pid_recycled", lambda pid, started: True)
        kb._append_event(conn, tid, "worker_bound",
                         {"pid": 9191, "claim_lock": lock, "started_at": "2026-10-21T09:00:00.000000"},
                         run_id=run1.id)
        assert kb.reclaim_task(
            conn, tid, reason="operator",
            signal_fn=lambda pid, sig: signalled.append((pid, int(sig)))) is True
        assert signalled == []  # a recycled pid is never signalled
        assert kb.get_task(conn, tid).status == "ready"
    finally:
        conn.close()
