"""Internal admission for dispatcher-launched local Kanban workers.

The only established fact is that this process consumed a single-use grant for
one exact active board/task/run/profile/claim.  No permission, QA/Company
decision, expectation, business authority, or evidence validity is implied.

TCB: dispatcher, trusted runtime code, and authoritative SQLite.  Arbitrary
Python/SQL, monkeypatching, malicious startup imports/plugins, process-memory
inspection, DB replacement, and host compromise are out of scope.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import weakref
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import MutableMapping, Optional

_LAUNCH_CAPABILITY_ENV = "HERMES_KANBAN_PRINCIPAL_LAUNCH_CAPABILITY"
_CAPABILITY_RE = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_STARTUP_TIMEOUT_SECONDS = 120
_EVENT_VERSION = 1


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class RuntimePrincipal:
    """Read-only admitted identity; only registry members are trusted."""

    board: str
    board_identity: str
    task_id: str
    run_id: int
    profile: str
    claim_lock: str
    issue_event_id: int


@dataclass(slots=True, repr=False)
class _CapturedLaunch:
    _raw: Optional[str]
    db_path: Path
    board: str
    task_id: str
    run_id: int
    profile: str
    claim_lock: str

    def _take_raw(self) -> Optional[str]:
        raw, self._raw = self._raw, None
        return raw

    def _clear(self) -> None:
        self._raw = None

    def __repr__(self) -> str:
        return (
            "_CapturedLaunch(capability=[REDACTED], "
            f"board={self.board!r}, task_id={self.task_id!r}, "
            f"run_id={self.run_id!r}, profile={self.profile!r})"
        )


_CURRENT_PRINCIPAL: ContextVar[Optional[RuntimePrincipal]] = ContextVar(
    "hermes_kanban_runtime_principal", default=None,
)
_REGISTERED_PRINCIPALS: weakref.WeakSet[RuntimePrincipal] = weakref.WeakSet()


def _canonical_board_identity(conn: sqlite3.Connection) -> str:
    for row in conn.execute("PRAGMA database_list").fetchall():
        name, filename = row[1], row[2]
        if name == "main":
            if not filename:
                raise ValueError("principal admission requires a file-backed board")
            return f"sqlite-file:{os.path.normcase(str(Path(filename).resolve()))}"
    raise ValueError("principal admission cannot identify the board")


def _payload(row: sqlite3.Row) -> dict:
    try:
        value = json.loads(row["payload"] or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _active_claim(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    run_id: int,
    profile: str,
    claim_lock: str,
    now: int,
) -> Optional[sqlite3.Row]:
    row = conn.execute(
        """
        SELECT t.status AS task_status, t.assignee AS task_profile,
               t.current_run_id, t.claim_lock AS task_claim_lock,
               t.claim_expires AS task_claim_expires,
               r.task_id AS run_task_id, r.profile AS run_profile,
               r.status AS run_status, r.claim_lock AS run_claim_lock,
               r.claim_expires AS run_claim_expires, r.ended_at
          FROM tasks t JOIN task_runs r ON r.id=? WHERE t.id=?
        """,
        (int(run_id), task_id),
    ).fetchone()
    if row is None:
        return None
    valid = (
        row["task_status"] == "running"
        and row["task_profile"] == profile
        and int(row["current_run_id"] or 0) == int(run_id)
        and row["task_claim_lock"] == claim_lock
        and int(row["task_claim_expires"] or 0) > now
        and row["run_task_id"] == task_id
        and row["run_profile"] == profile
        and row["run_status"] == "running"
        and row["run_claim_lock"] == claim_lock
        and int(row["run_claim_expires"] or 0) > now
        and row["ended_at"] is None
    )
    return row if valid else None


def _has_reference(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    run_id: int,
    kind: str,
    issue_event_id: int,
) -> bool:
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind=?",
        (task_id, int(run_id), kind),
    ).fetchall()
    for row in rows:
        try:
            if int(_payload(row).get("issue_event_id", -1)) == issue_event_id:
                return True
        except (TypeError, ValueError):
            continue
    return False


def issue_local_launch(
    conn: sqlite3.Connection,
    *,
    board: str,
    task_id: str,
    run_id: int,
    expected_profile: str,
    claim_lock: str,
    startup_timeout_seconds: int = DEFAULT_STARTUP_TIMEOUT_SECONDS,
) -> str:
    """Atomically store an exact binding and return its 256-bit raw grant."""
    from hermes_cli import kanban_db as kb

    board = kb._normalize_board_slug(board)
    task_id, expected_profile, claim_lock = (
        str(task_id or "").strip(),
        str(expected_profile or "").strip(),
        str(claim_lock or "").strip(),
    )
    run_id, timeout = int(run_id), int(startup_timeout_seconds)
    if not board or not task_id or run_id <= 0 or not expected_profile or not claim_lock or timeout <= 0:
        raise ValueError("complete positive launch binding is required")

    raw = secrets.token_hex(32)
    now = int(time.time())
    try:
        with kb.write_txn(conn):
            claim = _active_claim(
                conn, task_id=task_id, run_id=run_id,
                profile=expected_profile, claim_lock=claim_lock, now=now,
            )
            if claim is None:
                raise ValueError("launch claim is not current")
            deadline = min(
                now + timeout,
                int(claim["task_claim_expires"]),
                int(claim["run_claim_expires"]),
            )
            kb._append_event(
                conn, task_id, "principal_launch_issued",
                {
                    "version": _EVENT_VERSION,
                    "board": board,
                    "board_identity": _canonical_board_identity(conn),
                    "task_id": task_id,
                    "run_id": run_id,
                    "expected_profile": expected_profile,
                    "claim_lock": claim_lock,
                    "startup_deadline": deadline,
                    "capability_sha256": hashlib.sha256(raw.encode("ascii")).hexdigest(),
                },
                run_id=run_id,
            )
        return raw
    except Exception:
        raw = ""
        raise


def capture_startup_capability(
    environ: Optional[MutableMapping[str, str]] = None,
) -> Optional[_CapturedLaunch]:
    """Pop raw startup input immediately; malformed input yields no principal."""
    env = os.environ if environ is None else environ
    raw = env.pop(_LAUNCH_CAPABILITY_ENV, None)
    if not isinstance(raw, str) or not _CAPABILITY_RE.fullmatch(raw):
        raw = None
        return None
    try:
        db_path = str(env.get("HERMES_KANBAN_DB", "")).strip()
        board = str(env.get("HERMES_KANBAN_BOARD", "")).strip().lower()
        task_id = str(env.get("HERMES_KANBAN_TASK", "")).strip()
        profile = str(env.get("HERMES_PROFILE", "")).strip().lower()
        claim_lock = str(env.get("HERMES_KANBAN_CLAIM_LOCK", "")).strip()
        run_id = int(str(env.get("HERMES_KANBAN_RUN_ID", "")).strip())
        if not db_path or not board or not task_id or not profile or not claim_lock or run_id <= 0:
            raw = None
            return None
        captured = _CapturedLaunch(
            raw,
            db_path=Path(db_path),
            board=board,
            task_id=task_id,
            run_id=run_id,
            profile=profile,
            claim_lock=claim_lock,
        )
        raw = None
        return captured
    except (TypeError, ValueError, OSError):
        raw = None
        return None


def _register(binding: dict) -> RuntimePrincipal:
    principal = RuntimePrincipal(
        board=binding["board"],
        board_identity=binding["board_identity"],
        task_id=binding["task_id"],
        run_id=binding["run_id"],
        profile=binding["expected_profile"],
        claim_lock=binding["claim_lock"],
        issue_event_id=binding["issue_event_id"],
    )
    _REGISTERED_PRINCIPALS.add(principal)
    _CURRENT_PRINCIPAL.set(principal)
    return principal


def admit_captured_launch(captured: Optional[_CapturedLaunch]) -> Optional[RuntimePrincipal]:
    """Consume once under BEGIN IMMEDIATE; register only after commit succeeds."""
    if not isinstance(captured, _CapturedLaunch):
        return None
    raw = captured._take_raw()
    if raw is None:
        return None
    try:
        digest = hashlib.sha256(raw.encode("ascii")).hexdigest()
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_db_connect import connect_closing

        with connect_closing(db_path=captured.db_path) as conn:
            with kb.write_txn(conn):
                issue_row = issue = None
                rows = conn.execute(
                    "SELECT id, payload FROM task_events "
                    "WHERE task_id=? AND run_id=? AND kind='principal_launch_issued' "
                    "ORDER BY id DESC",
                    (captured.task_id, captured.run_id),
                ).fetchall()
                for candidate in rows:
                    candidate_payload = _payload(candidate)
                    if hmac.compare_digest(
                        str(candidate_payload.get("capability_sha256", "")), digest,
                    ):
                        issue_row, issue = candidate, candidate_payload
                        break
                if issue_row is None or issue is None:
                    return None

                issue_id = int(issue_row["id"])
                referenced = any(
                    _has_reference(
                        conn, task_id=captured.task_id, run_id=captured.run_id,
                        kind=kind, issue_event_id=issue_id,
                    )
                    for kind in ("principal_launch_consumed", "principal_launch_revoked")
                )
                if referenced:
                    return None

                now = int(time.time())
                board_identity = _canonical_board_identity(conn)
                try:
                    static_valid = (
                        int(issue.get("version", 0)) == _EVENT_VERSION
                        and issue.get("board") == captured.board
                        and issue.get("board_identity") == board_identity
                        and issue.get("task_id") == captured.task_id
                        and int(issue.get("run_id", 0)) == captured.run_id
                        and issue.get("expected_profile") == captured.profile
                        and issue.get("claim_lock") == captured.claim_lock
                        and int(issue.get("startup_deadline", 0)) >= now
                    )
                except (TypeError, ValueError):
                    static_valid = False
                if not static_valid or _active_claim(
                    conn, task_id=captured.task_id, run_id=captured.run_id,
                    profile=captured.profile, claim_lock=captured.claim_lock, now=now,
                ) is None:
                    return None

                consumed = {
                    "version": _EVENT_VERSION,
                    "issue_event_id": issue_id,
                    "board": captured.board,
                    "board_identity": board_identity,
                    "task_id": captured.task_id,
                    "run_id": captured.run_id,
                    "expected_profile": captured.profile,
                    "claim_lock": captured.claim_lock,
                }
                kb._append_event(
                    conn, captured.task_id, "principal_launch_consumed",
                    consumed, run_id=captured.run_id,
                )
            return _register(consumed)
    except Exception:
        return None
    finally:
        raw = None
        captured._clear()


def _is_registered_principal(value: object) -> bool:
    try:
        return value in _REGISTERED_PRINCIPALS
    except TypeError:
        return False


def current_principal() -> Optional[RuntimePrincipal]:
    """Return a registry principal, except inside delegated-child context."""
    try:
        from agent.delegation_context import is_delegated_child_process_context
        if is_delegated_child_process_context():
            return None
    except Exception:
        if os.environ.get("HERMES_DELEGATED_CHILD_CONTEXT"):
            return None
    principal = _CURRENT_PRINCIPAL.get()
    return principal if _is_registered_principal(principal) else None


def _clear_current_principal_for_tests() -> None:
    principal = _CURRENT_PRINCIPAL.get()
    _CURRENT_PRINCIPAL.set(None)
    if isinstance(principal, RuntimePrincipal):
        _REGISTERED_PRINCIPALS.discard(principal)
