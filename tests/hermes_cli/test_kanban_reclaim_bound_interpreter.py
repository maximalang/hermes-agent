"""Reclaim must also terminate the run's bound interpreter, not only the dispatcher's submitter.

2026-10-21 fleet-ops incident: TTL reclaim killed the spawned submitter while the
gateway-managed worker (the ``worker_bound`` interpreter) stayed alive as a ~200 MB zombie,
because it had wedged in startup before its own submitter-watch began. The signal keys on the
run's ``worker_bound`` pid + fingerprint: a recycled or unverified pid is never signalled."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
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


def test_stale_reclaim_signals_bound_interpreter(kanban_home, monkeypatch):
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


@pytest.mark.parametrize("mode", ["ttl", "stale"])
@pytest.mark.parametrize("identity,signal_error", [
    ("verified-child", False), ("verified-child", True),
    (kbd.UNVERIFIED_WORKER_FINGERPRINT, False), (None, False),
])
def test_automatic_reclaim_holds_claim_until_bound_child_is_gone(
    kanban_home, monkeypatch, mode, identity, signal_error,
):
    conn = kbc.connect()
    signalled = []
    try:
        tid, run = _claimed_task(conn)
        lock = kb.get_task(conn, tid).claim_lock
        kb._append_event(conn, tid, "worker_bound", {
            "pid": 9191, "claim_lock": lock, "started_at": identity,
        }, run_id=run.id)
        monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid == 9191)
        monkeypatch.setattr(kbd, "_pid_recycled", lambda pid, started: False)
        monkeypatch.setattr(kbd, "_poll_worker_exit", lambda pid, started: False)
        old = int(time.time()) - 30
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET claim_expires = ?, started_at = ? WHERE id = ?",
                         (old, old, tid))
            conn.execute("UPDATE task_runs SET started_at = ? WHERE id = ?", (old, run.id))

        def cannot_stop(pid, sig):
            assert not conn.in_transaction
            signalled.append(pid)
            if signal_error:
                raise PermissionError("denied")

        if mode == "ttl":
            assert kb.release_stale_claims(conn, signal_fn=cannot_stop) == 0
        else:
            assert kbd.detect_stale_running(conn, stale_timeout_seconds=1,
                                            signal_fn=cannot_stop) == []
        task = kb.get_task(conn, tid)
        assert task.status == "running" and task.current_run_id == run.id
        assert task.claim_lock == lock
        assert any(e.kind == "reclaim_deferred" for e in kb.list_events(conn, tid))
        if identity in (None, kbd.UNVERIFIED_WORKER_FINGERPRINT):
            assert not signalled
        # Once the process disappears the existing deferred retry can release it.
        monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET claim_expires = ? WHERE id = ?", (old, tid))
        if mode == "ttl":
            assert kb.release_stale_claims(conn, signal_fn=cannot_stop) == 1
        else:
            assert kbd.detect_stale_running(conn, stale_timeout_seconds=1,
                                            signal_fn=cannot_stop) == [tid]
    finally:
        conn.close()


def test_expired_ttl_terminates_bound_child_before_releasing_claim(kanban_home, monkeypatch):
    conn = kbc.connect()
    live = {9191}
    signalled = []
    try:
        tid, run = _claimed_task(conn)
        lock = kb.get_task(conn, tid).claim_lock
        kb._append_event(conn, tid, "worker_bound", {
            "pid": 9191, "claim_lock": lock, "started_at": "verified-child",
        }, run_id=run.id)
        monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid in live)
        monkeypatch.setattr(kbd, "_pid_recycled", lambda pid, started: False)
        monkeypatch.setattr(kbd, "_poll_worker_exit", lambda pid, started: pid not in live)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET claim_expires = ? WHERE id = ?",
                         (int(time.time()) - 30, tid))

        def stop_child(pid, sig):
            # Process operations must precede release, without holding the writer lock.
            assert not conn.in_transaction
            task = kb.get_task(conn, tid)
            assert task.status == "running" and task.current_run_id == run.id
            assert task.claim_lock == lock
            signalled.append(pid)
            live.discard(pid)

        assert kb.release_stale_claims(conn, signal_fn=stop_child) == 1
        assert signalled == [9191]
        assert not live
        assert kb.get_task(conn, tid).status == "ready"
    finally:
        conn.close()


@pytest.mark.parametrize("mode", ["ttl", "stale"])
def test_reclaim_snapshot_never_releases_a_replacement_run(kanban_home, monkeypatch, mode):
    conn = kbc.connect()
    try:
        tid, run = _claimed_task(conn)
        task = kb.get_task(conn, tid)
        kb._append_event(conn, tid, "worker_bound", {
            "pid": 9191, "claim_lock": task.claim_lock, "started_at": "verified-child",
        }, run_id=run.id)
        old = int(time.time()) - 30
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET claim_expires = ?, started_at = ? WHERE id = ?", (old, old, tid))
            conn.execute("UPDATE task_runs SET started_at = ? WHERE id = ?", (old, run.id))
        monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid == 9191)
        monkeypatch.setattr(kbd, "_pid_recycled", lambda pid, started: False)
        monkeypatch.setattr(kbd, "_poll_worker_exit", lambda pid, started: True)
        replacement = []

        def replace_attempt(pid, sig):
            # Same operator identity may be reused across claims; the run fence is necessary.
            assert not conn.in_transaction
            with kb.write_txn(conn):
                kb._end_run(conn, tid, outcome="reclaimed", status="reclaimed", error="concurrent recovery")
                conn.execute("UPDATE tasks SET status = 'ready', claim_lock = NULL WHERE id = ?", (tid,))
            assert kb.claim_task(conn, tid, claimer=task.claim_lock)
            replacement.append(kb.get_task(conn, tid).current_run_id)
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET claim_expires = ? WHERE id = ?", (old, tid))

        if mode == "ttl":
            assert kb.release_stale_claims(conn, signal_fn=replace_attempt) == 0
        else:
            assert kbd.detect_stale_running(conn, stale_timeout_seconds=1, signal_fn=replace_attempt) == []
        current = kb.get_task(conn, tid)
        assert replacement and current.current_run_id == replacement[0]
        assert current.status == "running" and current.claim_lock == task.claim_lock
    finally:
        conn.close()


def test_late_bind_between_lookup_and_release_cannot_leave_a_live_child(kanban_home, monkeypatch):
    conn = kbc.connect()
    try:
        tid, run = _claimed_task(conn)
        old = int(time.time()) - 30
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET claim_expires = ? WHERE id = ?", (old, tid))
        original = kbd._signal_bound_interpreter
        bound = []
        live = {9191}
        monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid in live)
        monkeypatch.setattr(kbd, "_pid_recycled", lambda pid, started: False)
        monkeypatch.setattr(kbd, "_poll_worker_exit", lambda pid, started: pid not in live)

        def late_bind(conn, task_id, run_id, **kwargs):
            result = original(conn, task_id, run_id, **kwargs)
            if not bound:
                # bind_worker_context checks this same run+claim under the writer lock.
                with kb.write_txn(conn):
                    task = kb.get_task(conn, tid)
                    if task.status == "running" and task.current_run_id == run.id:
                        kb._append_event(conn, tid, "worker_bound", {
                            "pid": 9191, "claim_lock": task.claim_lock, "started_at": "verified-child",
                        }, run_id=run.id)
                        bound.append(True)
            return result

        monkeypatch.setattr(kbd, "_signal_bound_interpreter", late_bind)
        reclaimed = kb.release_stale_claims(conn, signal_fn=lambda pid, sig: live.discard(pid))
        task = kb.get_task(conn, tid)
        assert bound
        assert not (reclaimed and task.status != "running" and live)
    finally:
        conn.close()


def test_managed_unknown_owner_receipt_is_preserved(kanban_home, monkeypatch):
    conn = kbc.connect()
    try:
        tid, run = _claimed_task(conn)
        owner_path = kanban_home / "owner-receipt.db"
        with sqlite3.connect(owner_path) as owner:
            owner.execute("CREATE TABLE session_admissions (target_session_id TEXT, request_id TEXT, status TEXT, outcome TEXT)")
            owner.execute("INSERT INTO session_admissions VALUES ('session', 'request', 'unknown', NULL)")
        kb._append_event(conn, tid, "owner_admitted", {
            "db": str(owner_path), "session_id": "session", "request_id": "request",
            "pid": os.getpid(), "birth": 0,
        }, run_id=run.id)
        lock = kb.get_task(conn, tid).claim_lock
        kb._append_event(conn, tid, "worker_bound", {
            "pid": 9191, "claim_lock": lock, "started_at": "verified-child",
        }, run_id=run.id)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET claim_expires = ? WHERE id = ?", (int(time.time()) - 30, tid))
        signals = []
        assert kb.release_stale_claims(conn, signal_fn=lambda pid, sig: signals.append(pid)) == 0
        assert not signals
        assert kb.get_task(conn, tid).current_run_id == run.id
        assert kb.get_task(conn, tid).claim_lock == lock
    finally:
        conn.close()


def test_terminal_reaper_retries_bound_child_after_submitter_evidence_is_cleared(kanban_home, monkeypatch):
    conn = kbc.connect()
    try:
        tid, run = _claimed_task(conn)
        kb._append_event(conn, tid, "worker_bound", {
            "pid": 9191, "claim_lock": kb.get_task(conn, tid).claim_lock,
            "started_at": "verified-child",
        }, run_id=run.id)
        assert kb.complete_task(conn, tid, result="artifact verified", expected_run_id=run.id)
        with kb.write_txn(conn):
            conn.execute("UPDATE task_runs SET ended_at = ? WHERE id = ?", (int(time.time()) - 600, run.id))
        live = {9191}
        monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid in live)
        monkeypatch.setattr(kbd, "_pid_recycled", lambda pid, started: False)
        monkeypatch.setattr(kbd, "_poll_worker_exit", lambda pid, started: pid not in live)
        # Failed termination leaves durable evidence for the existing next tick.
        def denied(pid, sig):
            raise PermissionError("test denial")
        assert kbd.reap_terminal_workers(conn, signal_fn=denied) == []
        assert live == {9191}
        assert kbd.reap_terminal_workers(conn, signal_fn=lambda pid, sig: live.discard(pid)) == [tid]
        assert not live
    finally:
        conn.close()


@pytest.mark.parametrize("mode", ["ttl", "stale"])
@pytest.mark.parametrize("bad_binding", [
    "invalid-json", "list", "null", "no-pid", "string-pid", "bool-pid",
    "negative-pid", "no-lock", "wrong-lock", "probe-denied",
])
def test_uncertain_bound_identity_holds_only_its_task(kanban_home, monkeypatch, mode, bad_binding):
    conn = kbc.connect()
    try:
        tid, run = _claimed_task(conn)
        healthy, healthy_run = _claimed_task(conn)
        lock = kb.get_task(conn, tid).claim_lock
        payload = {"pid": 9191, "claim_lock": lock, "started_at": "verified-child"}
        if bad_binding == "no-pid":
            payload.pop("pid")
        elif bad_binding == "string-pid":
            payload["pid"] = "not-a-pid"
        elif bad_binding == "bool-pid":
            payload["pid"] = True
        elif bad_binding == "negative-pid":
            payload["pid"] = -9191
        elif bad_binding == "no-lock":
            payload.pop("claim_lock")
        elif bad_binding == "wrong-lock":
            payload["claim_lock"] = lock + "-other-run"
        kb._append_event(conn, tid, "worker_bound", payload, run_id=run.id)
        old = int(time.time()) - 30
        with kb.write_txn(conn):
            for task_id, run_id in ((tid, run.id), (healthy, healthy_run.id)):
                conn.execute("UPDATE tasks SET started_at = ?, claim_expires = ? WHERE id = ?",
                             (old, old, task_id))
                conn.execute("UPDATE task_runs SET started_at = ? WHERE id = ?", (old, run_id))
            if bad_binding in ("invalid-json", "list", "null"):
                raw = {"invalid-json": "{bad", "list": "[]", "null": "null"}[bad_binding]
                conn.execute("UPDATE task_events SET payload = ? WHERE task_id = ? AND kind = 'worker_bound'",
                             (raw, tid))
        signals = []

        def alive(pid):
            if pid == 9191 and bad_binding == "probe-denied":
                raise PermissionError("process inspection denied")
            return pid == 9191

        monkeypatch.setattr(kb, "_pid_alive", alive)
        monkeypatch.setattr(kbd, "_pid_recycled", lambda *args: False)
        if mode == "ttl":
            assert kb.release_stale_claims(conn, signal_fn=lambda *args: signals.append(args)) == 1
        else:
            assert kbd.detect_stale_running(conn, stale_timeout_seconds=1,
                                           signal_fn=lambda *args: signals.append(args)) == [healthy]
        assert not signals
        assert kb.get_task(conn, tid).status == "running"
        assert kb.get_task(conn, tid).current_run_id == run.id
        assert kb.get_task(conn, healthy).status == "ready"
        assert any(e.kind == "reclaim_deferred" for e in kb.list_events(conn, tid))
    finally:
        conn.close()


def test_terminal_reaper_never_acknowledges_a_foreign_host_pid(kanban_home, monkeypatch):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="foreign-bound", assignee="worker")
        assert kb.claim_task(conn, tid, claimer="foreign-host:claim")
        run = kb.latest_run(conn, tid)
        kb._append_event(conn, tid, "worker_bound", {
            "pid": 9191, "claim_lock": "foreign-host:claim", "started_at": "foreign-boot|1",
        }, run_id=run.id)
        assert kb.complete_task(conn, tid, result="remote artifact", expected_run_id=run.id)
        with kb.write_txn(conn):
            conn.execute("UPDATE task_runs SET ended_at = ? WHERE id = ?", (int(time.time()) - 600, run.id))
        monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
        assert kbd.reap_terminal_workers(conn, signal_fn=lambda *args: pytest.fail("foreign signal")) == []
        assert not any(e.kind == "terminal_worker_reaped" for e in kb.list_events(conn, tid))
    finally:
        conn.close()


def test_unreadable_live_identity_is_not_proof_of_pid_reuse(monkeypatch):
    signals = []
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(kbd, "_process_fingerprint", lambda pid: None)
    result = kbd._terminate_reclaimed_worker(
        9191, kb._claimer_id(), started_at="original-boot|1234",
        signal_fn=lambda pid, sig: signals.append(pid),
    )
    assert not signals
    assert not result["terminated"]
    assert result.get("signal_refused")
    assert kbd._worker_alive(9191, "original-boot|1234")


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


def test_defer_budget_ends_series_but_never_releases_claim(kanban_home, monkeypatch):
    """t_4fcd3e44: an unverifiable identity refuses termination forever, so the
    defer series is budgeted — past the budget one marker replaces the per-tick
    events and the claim is still extended (never released: a duplicate spawn
    beside a possibly-live worker stays forbidden)."""
    from hermes_cli import kanban_db_reaping as kbr
    conn = kbc.connect()
    try:
        tid, run = _claimed_task(conn)
        lock = kb.get_task(conn, tid).claim_lock
        kb._append_event(conn, tid, "worker_bound",
                         {"pid": 9191, "claim_lock": lock,
                          "started_at": kbd.UNVERIFIED_WORKER_FINGERPRINT},
                         run_id=run.id)
        monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid == 9191)
        monkeypatch.setattr(kbd, "_pid_alive", lambda pid: pid == 9191)
        monkeypatch.setattr(kbr, "RECLAIM_DEFER_MAX_CONSECUTIVE", 3)
        monkeypatch.setattr(kbr, "RECLAIM_DEFER_MAX_AGE_SECONDS", 6 * 3600)
        old = int(time.time()) - 30
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET claim_expires = ?, started_at = ? WHERE id = ?",
                         (old, old, tid))
            conn.execute("UPDATE task_runs SET started_at = ? WHERE id = ?", (old, run.id))

        for cycle in range(6):  # 3 budgeted defers, then one marker, then silence
            assert kb.release_stale_claims(conn, signal_fn=lambda pid, sig: None) == 0
            task = kb.get_task(conn, tid)
            assert task.status == "running" and task.claim_lock == lock  # never released
            assert task.claim_expires > old  # still extended: held, not parked
            kinds = [e.kind for e in kb.list_events(conn, tid)]
            if cycle < 3:
                assert kinds.count("reclaim_deferred") == cycle + 1
                assert "reclaim_defer_budget_exhausted" not in kinds
            else:
                assert kinds.count("reclaim_deferred") == 3  # series ended: no flood
                assert kinds.count("reclaim_defer_budget_exhausted") == 1
            with kb.write_txn(conn):  # next tick: the defer grace elapses again
                conn.execute("UPDATE tasks SET claim_expires = ? WHERE id = ?", (old, tid))

        marker = next(e for e in kb.list_events(conn, tid)
                      if e.kind == "reclaim_defer_budget_exhausted")
        payload = marker.payload
        assert payload["deferred_count"] == 3
        assert payload["refusal_reason"] == "unverified_worker_fingerprint"
    finally:
        conn.close()


def test_defer_budget_age_deadline_trips_from_first_defer(kanban_home, monkeypatch):
    """The deadline arm: a short series whose FIRST defer is older than the age
    budget marks on the next cycle even well below the consecutive bound."""
    from hermes_cli import kanban_db_reaping as kbr
    conn = kbc.connect()
    try:
        tid, run = _claimed_task(conn)
        lock = kb.get_task(conn, tid).claim_lock
        kb._append_event(conn, tid, "worker_bound",
                         {"pid": 9191, "claim_lock": lock,
                          "started_at": kbd.UNVERIFIED_WORKER_FINGERPRINT},
                         run_id=run.id)
        monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid == 9191)
        monkeypatch.setattr(kbd, "_pid_alive", lambda pid: pid == 9191)
        monkeypatch.setattr(kbr, "RECLAIM_DEFER_MAX_CONSECUTIVE", 100)  # count won't trip
        monkeypatch.setattr(kbr, "RECLAIM_DEFER_MAX_AGE_SECONDS", 3600)
        old = int(time.time()) - 30
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET claim_expires = ?, started_at = ? WHERE id = ?",
                         (old, old, tid))
            conn.execute("UPDATE task_runs SET started_at = ? WHERE id = ?", (old, run.id))
        for _ in range(2):  # two ordinary defers inside the budget
            assert kb.release_stale_claims(conn, signal_fn=lambda pid, sig: None) == 0
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET claim_expires = ? WHERE id = ?", (old, tid))
        with kb.write_txn(conn):  # age the first defer past the deadline
            conn.execute(
                "UPDATE task_events SET created_at = created_at - 7200 WHERE id = "
                "(SELECT MIN(id) FROM task_events WHERE task_id = ? "
                " AND kind = 'reclaim_deferred')", (tid,))
        assert kb.release_stale_claims(conn, signal_fn=lambda pid, sig: None) == 0
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert kinds.count("reclaim_deferred") == 2
        assert kinds.count("reclaim_defer_budget_exhausted") == 1
        task = kb.get_task(conn, tid)
        assert task.status == "running" and task.claim_lock == lock  # still held
    finally:
        conn.close()
