"""Bound managed-interpreter cleanup and terminal-run reaping.

The dispatcher remains the compatibility namespace; process identity/termination
helpers are resolved there at call time so existing test hooks remain authoritative.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Optional

import psutil

def _signal_bound_interpreter(
    conn: sqlite3.Connection, task_id: str, run_id, *, signal_fn=None,
) -> dict[str, Any]:
    """Best-effort termination of the run's BOUND interpreter — the gateway-managed worker
    that recorded itself via ``worker_bound`` (pid + spawn fingerprint). The dispatcher's
    spawned submitter is a different process tree: killing it alone left the gateway child
    alive as a zombie when the child had wedged in startup before its own submitter-watch
    began (2026-10-21). Keys on the LATEST ``worker_bound`` of THIS run only, so a recycled
    or unverified pid is never signalled; empty dict when the run never bound one."""
    # Late binding: kanban_db and its dispatch sibling import each other's helpers.
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_dispatch as _kbd
    info: dict[str, Any] = {"bound_event_id": None}
    if not run_id:
        return info
    row = conn.execute(
        "SELECT e.id, e.payload, r.claim_lock AS run_lock FROM task_events e "
        "LEFT JOIN task_runs r ON r.id = e.run_id AND r.task_id = e.task_id "
        "WHERE e.task_id = ? AND e.run_id = ? AND e.kind = 'worker_bound' "
        "ORDER BY e.id DESC LIMIT 1", (task_id, run_id),
    ).fetchone()
    if row is None:
        return info
    info["bound_event_id"] = row["id"]
    identity = _bound_identity(row)
    refusal = {**info, "signal_refused": True, "terminated": False}
    if identity is None:
        return {**refusal, "refusal_reason": "bound_identity_unreadable"}
    pid, lock, started_at = identity
    # PID numbers are meaningful only on the run's host, even when absent here.
    if not lock.startswith(_kb._host_prefix()) or pid == os.getpid():
        return {**refusal, "refusal_reason": "bound_identity_not_local"}
    try:
        if not _kb._pid_alive(pid):
            return {**info, "host_local": True, "terminated": True}
        termination = _kbd._terminate_reclaimed_worker(
            pid, lock, signal_fn=signal_fn, started_at=started_at)
    except (OSError, psutil.Error):
        return {**refusal, "refusal_reason": "bound_process_unreadable"}
    return {**info, **termination}


def _bound_identity(row):
    """Incomplete evidence is not permission to release or signal a PID."""
    from hermes_cli import kanban_db_dispatch as _kbd
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    pid, lock = payload.get("pid"), payload.get("claim_lock")
    if (type(pid) is not int or pid <= 0 or not isinstance(lock, str)
            or not lock or lock != row["run_lock"]):
        return None
    return pid, lock, payload.get("started_at") or _kbd.UNVERIFIED_WORKER_FINGERPRINT


def reap_terminal_workers(conn: sqlite3.Connection, *, signal_fn=None) -> list[str]:
    """End host-local workers that outlived their run (issue #111791) — a worker
    that called ``kanban_complete`` and then hung keeps its ``state.db`` sidecar
    fds open and no ``running``-only sweep can see it once ``tasks.worker_pid`` is
    cleared. Keys on the closed ``task_runs`` row's retained pid + spawn
    fingerprint: a legacy row (NULL fingerprint) or a recycled PID is never
    signalled; a pid that is simply gone just has its evidence cleared. A run
    that ended less than ``_kbd.TERMINAL_WORKER_REAP_GRACE_SECONDS`` ago is left
    alone so a worker still finalising after its own transition is not killed.
    One row's failure (signal, /proc probe) is logged and skips only that row.
    Returns the task ids whose worker was terminated."""
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_dispatch as _kbd
    rows = conn.execute(
        "SELECT r.id, r.task_id, r.worker_pid, r.worker_started_at, r.claim_lock, b.id AS bound_id "
        "FROM task_runs r LEFT JOIN task_events b ON b.id = "
        "(SELECT MAX(id) FROM task_events WHERE run_id = r.id AND kind = 'worker_bound') "
        "AND NOT EXISTS (SELECT 1 FROM task_events e WHERE e.run_id = r.id "
        "    AND e.kind = 'terminal_worker_reaped' "
        "    AND json_extract(e.payload, '$.bound_event_id') = b.id) "
        "WHERE r.ended_at IS NOT NULL AND r.ended_at <= ? "
        "AND ((r.worker_pid IS NOT NULL AND r.worker_started_at IS NOT NULL) OR b.id IS NOT NULL)",
        (int(time.time()) - _kbd.TERMINAL_WORKER_REAP_GRACE_SECONDS,),
    ).fetchall()
    host_prefix = _kb._host_prefix()
    reaped: list[str] = []
    for row in rows:
        try:
            if row["bound_id"] is not None:
                bound = _kbd._signal_bound_interpreter(conn, row["task_id"], row["id"], signal_fn=signal_fn)
                if bound.get("terminated"):
                    with _kb.write_txn(conn):
                        _kb._append_event(conn, row["task_id"], "terminal_worker_reaped", bound, run_id=row["id"])
                    if bound.get("termination_attempted"):
                        reaped.append(row["task_id"])
            if row["worker_pid"] is not None and row["worker_started_at"] is not None:
                _reap_terminal_worker_row(conn, row, host_prefix, signal_fn, reaped)
        except Exception:
            _kb._log.debug(
                "kanban dispatch: terminal worker reap failed for run %s (task %s)",
                row["id"], row["task_id"], exc_info=True,
            )
    return reaped


def _reap_terminal_worker_row(conn, row, host_prefix: str, signal_fn, reaped: list[str]) -> None:
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_dispatch as _kbd
    pid, fingerprint = int(row["worker_pid"]), row["worker_started_at"]
    if pid == os.getpid() or not str(row["claim_lock"] or "").startswith(host_prefix):
        return
    if fingerprint == _kbd.UNVERIFIED_WORKER_FINGERPRINT and _kb._pid_alive(pid):
        return  # unproven identity: never signalled; its evidence is cleared once the pid is gone
    alive = _kbd._worker_alive(pid, fingerprint)
    termination = None
    if alive:
        termination = _kbd._terminate_reclaimed_worker(
            pid, row["claim_lock"], signal_fn=signal_fn, started_at=fingerprint)
        if not termination["terminated"]:
            return  # still alive: try again next tick
    with _kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET worker_pid = NULL, worker_started_at = NULL "
            "WHERE id = ? AND worker_pid = ? AND worker_started_at = ?",
            (row["id"], pid, fingerprint),
        )
        if alive:
            _kb._append_event(
                conn, row["task_id"], "terminal_worker_reaped",
                {"pid": pid, "worker_started_at": fingerprint, **termination}, run_id=row["id"],
            )
    if alive and row["task_id"] not in reaped:
        reaped.append(row["task_id"])


def _record_reclaim(
    conn: sqlite3.Connection, task_id: str, termination: dict, *, error: str, payload: dict,
) -> Optional[int]:
    """Close the active run as ``reclaimed`` and emit the ``reclaimed`` event
    (payload merged with the termination report). Caller holds the txn."""
    from hermes_cli import kanban_db as _kb
    run_id = _kb._end_run(
        conn, task_id, outcome="reclaimed", status="reclaimed", error=error, metadata=termination,
    )
    payload.update(termination)
    _kb._append_event(conn, task_id, "reclaimed", payload, run_id=run_id)
    return run_id



def _extend_live_stale_claim(conn: sqlite3.Connection, row: sqlite3.Row, now: int) -> None:
    """TTL-expired claim whose host-local worker is alive: extend instead of
    reclaiming (``claim_extended`` event). CAS on the same expired lock so a
    concurrent reclaimer wins cleanly."""
    from hermes_cli import kanban_db as _kb
    new_expires = now + _kb._resolve_claim_ttl_seconds()
    with _kb.write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET claim_expires = ? "
            "WHERE id = ? AND status = 'running' "
            "  AND claim_lock IS ? "
            "  AND claim_expires IS NOT NULL "
            "  AND claim_expires < ?", (new_expires, row["id"], row["claim_lock"], now),
        )
        if cur.rowcount != 1:
            return
        run_id = _kb._extend_run_claim(conn, row["id"], new_expires)
        _kb._append_event(
            conn, row["id"], "claim_extended",
            {
                "reason": "pid_alive",
                "worker_pid": int(row["worker_pid"]),
                "claim_lock": row["claim_lock"],
                "claim_expires_was": _kb._to_epoch(row["claim_expires"]),
                "claim_expires_now": new_expires,
                "last_heartbeat_at": _kb._to_epoch(row["last_heartbeat_at"]),
            },
            run_id=run_id,
        )
