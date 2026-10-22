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

# Reclaim-deferral budget. An identity this host can never verify (foreign-host
# lock, unreadable payload, unverified pid in use) refuses termination forever,
# so the per-tick defer series is budgeted: once either bound trips, one
# ``reclaim_defer_budget_exhausted`` marker replaces the per-tick events and the
# claim is held silently (extend only) — release stays forbidden beside a
# possibly-live worker; diagnostics point an operator at the card.
RECLAIM_DEFER_MAX_CONSECUTIVE = 24
RECLAIM_DEFER_MAX_AGE_SECONDS = 2 * 3600

# Reap-refusal budget. A refused reap can never succeed from this host, so each
# refusal appends one ``bound_reap_refused`` marker and the row leaves the reap
# scan while the latest marker is fresh (retry backoff) and permanently once the
# attempt budget is spent. A ``terminal_worker_reaped`` for the same identity
# clears the state (any host may append it).
REAP_REFUSAL_MAX_ATTEMPTS = 3
REAP_REFUSAL_RETRY_QUIET_SECONDS = 600


def _log_refusal(site: str, reason: str, **ctx: Any) -> None:
    """Debug visibility for identity refusals. Durable visibility lives in the
    ``bound_reap_refused`` / ``reclaim_defer_budget_exhausted`` markers and the
    diagnostics rules reading them."""
    from hermes_cli import kanban_db as _kb
    _kb._log.debug("kanban %s: refused (%s)%s", site, reason,
                   f" {ctx}" if ctx else "")


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
        _log_refusal("reap bound interpreter", "bound_identity_unreadable",
                     task_id=task_id, run_id=run_id)
        return {**refusal, "refusal_reason": "bound_identity_unreadable"}
    pid, lock, started_at = identity
    # PID numbers are meaningful only on the run's host, even when absent here.
    if not lock.startswith(_kb._host_prefix()) or pid == os.getpid():
        _log_refusal("reap bound interpreter", "bound_identity_not_local",
                     task_id=task_id, run_id=run_id, pid=pid)
        return {**refusal, "refusal_reason": "bound_identity_not_local"}
    try:
        if not _kb._pid_alive(pid):
            return {**info, "host_local": True, "terminated": True}
        termination = _kbd._terminate_reclaimed_worker(
            pid, lock, signal_fn=signal_fn, started_at=started_at)
    except (OSError, psutil.Error):
        _log_refusal("reap bound interpreter", "bound_process_unreadable",
                     task_id=task_id, run_id=run_id, pid=pid)
        return {**refusal, "refusal_reason": "bound_process_unreadable"}
    if termination.get("signal_refused") and not termination.get("terminated"):
        _log_refusal("reap bound interpreter",
                     termination.get("refusal_reason") or "signal_refused",
                     task_id=task_id, run_id=run_id, pid=pid)
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
    A refusal this host can never satisfy (foreign lock, unreadable identity,
    unverified pid in use) is marked ``bound_reap_refused`` so the row leaves the
    sample instead of being re-judged every tick (bounded retries). One row's
    failure (signal, /proc probe) is logged and skips only that row.
    Returns the task ids whose worker was terminated."""
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_dispatch as _kbd
    now = int(time.time())
    quiet = now - REAP_REFUSAL_RETRY_QUIET_SECONDS
    rows = conn.execute(
        "SELECT r.id, r.task_id, r.worker_pid, r.worker_started_at, r.claim_lock, b.id AS bound_id "
        "FROM task_runs r LEFT JOIN task_events b ON b.id = "
        "(SELECT MAX(id) FROM task_events WHERE run_id = r.id AND kind = 'worker_bound') "
        "AND NOT EXISTS (SELECT 1 FROM task_events e WHERE e.run_id = r.id "
        "    AND e.kind = 'terminal_worker_reaped' "
        "    AND json_extract(e.payload, '$.bound_event_id') = b.id) "
        "AND NOT EXISTS (SELECT 1 FROM task_events e WHERE e.run_id = r.id "
        "    AND e.kind = 'bound_reap_refused' "
        "    AND json_extract(e.payload, '$.bound_event_id') = b.id "
        "    GROUP BY e.run_id HAVING COUNT(*) >= ? OR MAX(e.created_at) > ?) "
        "WHERE r.ended_at IS NOT NULL AND r.ended_at <= ? "
        "AND ((r.worker_pid IS NOT NULL AND r.worker_started_at IS NOT NULL) OR b.id IS NOT NULL) "
        "AND NOT EXISTS (SELECT 1 FROM task_events e WHERE e.run_id = r.id "
        "    AND e.kind = 'bound_reap_refused' "
        "    AND json_extract(e.payload, '$.pid') = r.worker_pid "
        "    AND json_extract(e.payload, '$.worker_started_at') = r.worker_started_at "
        "    GROUP BY e.run_id HAVING COUNT(*) >= ? OR MAX(e.created_at) > ?)",
        (REAP_REFUSAL_MAX_ATTEMPTS, quiet,
         now - _kbd.TERMINAL_WORKER_REAP_GRACE_SECONDS,
         REAP_REFUSAL_MAX_ATTEMPTS, quiet),
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
                elif bound.get("signal_refused"):
                    _record_reap_refusal(conn, row, reason=bound.get("refusal_reason") or "signal_refused",
                                         bound_event_id=row["bound_id"])
            if row["worker_pid"] is not None and row["worker_started_at"] is not None:
                _reap_terminal_worker_row(conn, row, host_prefix, signal_fn, reaped)
        except Exception:  # health: allow BLE001 -- one row's failure (signal, /proc probe) must not abort the sweep; the debug log below carries exc_info=True
            _kb._log.debug(
                "kanban dispatch: terminal worker reap failed for run %s (task %s)",
                row["id"], row["task_id"], exc_info=True,
            )
    return reaped


def _record_reap_refusal(
    conn: sqlite3.Connection, row, *, reason: str,
    bound_event_id=None, pid=None, worker_started_at=None,
) -> None:
    """Append one ``bound_reap_refused`` marker (reason + bounded attempt
    count/age) so the reap scan can back off and finally drop the row, and
    diagnostics can see the identity nobody resolved."""
    from hermes_cli import kanban_db as _kb
    where = "task_id = ? AND run_id IS ? AND kind = 'bound_reap_refused'"
    params: list[Any] = [row["task_id"], row["id"]]
    if bound_event_id is not None:
        where += " AND json_extract(payload, '$.bound_event_id') = ?"
        params.append(bound_event_id)
    else:
        where += (" AND json_extract(payload, '$.pid') IS ?"
                  " AND json_extract(payload, '$.worker_started_at') IS ?")
        params += [pid, worker_started_at]
    agg = conn.execute(
        f"SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM task_events WHERE {where}",
        params,
    ).fetchone()
    attempt, first_ts, last_ts = int(agg[0]), int(agg[1] or 0), int(agg[2] or 0)
    _log_refusal("reap marker", reason, task_id=row["task_id"], run_id=row["id"],
                 attempt=attempt + 1)
    with _kb.write_txn(conn):
        _kb._append_event(conn, row["task_id"], "bound_reap_refused", {
            "refusal_reason": reason,
            "bound_event_id": bound_event_id,
            "pid": pid,
            "worker_started_at": worker_started_at,
            "attempt": attempt + 1,
            "first_refused_at": first_ts or last_ts,
            "last_refused_at": last_ts,
        }, run_id=row["id"])


def _reap_terminal_worker_row(conn, row, host_prefix: str, signal_fn, reaped: list[str]) -> None:
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_dispatch as _kbd
    pid, fingerprint = int(row["worker_pid"]), row["worker_started_at"]
    if pid == os.getpid():
        return
    lock = str(row["claim_lock"] or "")
    if not lock.startswith(host_prefix):
        # Foreign (or missing) lock: pid numbers from another host prove nothing
        # here and never will — mark so the row can leave the scan.
        _record_reap_refusal(conn, row,
                             reason="claim_lock_not_local" if lock else "claim_lock_missing",
                             pid=pid, worker_started_at=fingerprint)
        return
    if fingerprint == _kbd.UNVERIFIED_WORKER_FINGERPRINT and _kb._pid_alive(pid):
        # Unproven identity: never signalled. The live pid may be an innocent
        # reused number owned by a long-lived process (worst case: the worker is
        # dead and only its number survives) — the marker makes that state
        # detectable; retries inside the budget still clear the evidence once
        # the pid is gone.
        _record_reap_refusal(conn, row, reason="unverified_pid_in_use",
                             pid=pid, worker_started_at=fingerprint)
        return
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


def _worker_survived_termination(termination: dict) -> bool:
    """True when a worker we cannot prove dead may still be running.

    Releasing the claim then would spawn a second worker beside the first —
    the duplication loop. Two cases qualify: (a) we signalled our own
    host-local worker and it is still alive, and (b) the signal was refused
    (``signal_refused``) — an identity we could not verify, a non-local lock,
    or the current process itself. A refusal is no proof of death, so the
    claim is held (defer) until the run's own host or an operator resolves
    it; only a proven termination (``terminated``) may fall through to the
    release path.
    """
    return bool(
        (termination.get("signal_refused")
         or (termination.get("host_local") and termination.get("termination_attempted")))
        and not termination.get("terminated")
    )


_DEFER_SERIES_BREAKERS = {
    "claimed", "spawned", "worker_bound", "reclaimed", "completed",
    "reclaim_defer_budget_exhausted",
}


def _defer_series_state(conn: sqlite3.Connection, task_id: str, run_id) -> tuple[int, int]:
    """Trailing ``reclaim_deferred`` series for one run: ``(count, first_ts)``.
    A lifecycle event (claim/spawn/bind/reclaim/complete, or the budget marker)
    ends the series; interleaved heartbeats or comments do not."""
    rows = conn.execute(
        "SELECT kind, created_at FROM task_events WHERE task_id = ? AND run_id IS ? "
        "ORDER BY id DESC LIMIT 512", (task_id, run_id),
    ).fetchall()
    count, first_ts = 0, 0
    for row in rows:
        kind = row["kind"]
        if kind == "reclaim_deferred":
            count += 1
            first_ts = int(row["created_at"] or 0)
        elif kind in _DEFER_SERIES_BREAKERS:
            break
    return count, first_ts


def _defer_reclaim_for_live_worker(
    conn: sqlite3.Connection,
    task_id: str,
    claim_lock: Optional[str],
    now: int,
    termination: dict,
    *,
    reason: str,
    expected_run_id,
) -> None:
    """Hold a claim whose worker survived termination instead of releasing it.

    Extends ``claim_expires`` by ``RECLAIM_DEFER_GRACE_SECONDS`` so the task
    stays ``running`` (no duplicate spawn) and records ``reclaim_deferred``.
    The next tick retries the kill; not spawning a duplicate is what lets the
    throttled worker finally die.

    An identity this host can never verify refuses forever, so the series is
    budgeted (``RECLAIM_DEFER_MAX_CONSECUTIVE`` consecutive defers or
    ``RECLAIM_DEFER_MAX_AGE_SECONDS`` since the first): past the budget one
    ``reclaim_defer_budget_exhausted`` marker replaces the per-tick events and
    the claim is extended silently thereafter — the card is never released
    beside a possibly-live worker, and diagnostics surface it to an operator.
    """
    from hermes_cli import kanban_db as _kb
    grace = now + _kb.RECLAIM_DEFER_GRACE_SECONDS
    with _kb.write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET claim_expires = ? "
            "WHERE id = ? AND status = 'running' AND claim_lock IS ? AND current_run_id IS ?",
            (grace, task_id, claim_lock, expected_run_id),
        )
        if cur.rowcount != 1:
            return
        run_id = _kb._current_run_id(conn, task_id)
        if run_id is not None:
            conn.execute("UPDATE task_runs SET claim_expires = ? WHERE id = ?", (grace, run_id))
        payload = {"reason": reason, "claim_lock": claim_lock, "claim_expires_now": grace}
        payload.update(termination)
        marked = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND run_id IS ? "
            "AND kind = 'reclaim_defer_budget_exhausted' LIMIT 1",
            (task_id, expected_run_id),
        ).fetchone() is not None
        if marked:
            return  # budget already announced: hold silently (extend only)
        count, first_ts = _defer_series_state(conn, task_id, expected_run_id)
        if (count >= RECLAIM_DEFER_MAX_CONSECUTIVE
                or (first_ts and now - first_ts >= RECLAIM_DEFER_MAX_AGE_SECONDS)):
            _log_refusal("reclaim defer budget", payload.get("refusal_reason") or reason,
                         task_id=task_id, run_id=expected_run_id, deferred_count=count)
            _kb._append_event(conn, task_id, "reclaim_defer_budget_exhausted", {
                **payload,
                "deferred_count": count,
                "first_deferred_at": first_ts,
                "budget_max_consecutive": RECLAIM_DEFER_MAX_CONSECUTIVE,
                "budget_max_age_seconds": RECLAIM_DEFER_MAX_AGE_SECONDS,
            }, run_id=run_id)
            return
        _kb._append_event(conn, task_id, "reclaim_deferred", payload, run_id=run_id)



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
