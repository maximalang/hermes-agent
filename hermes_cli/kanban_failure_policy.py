"""Typed infrastructure holds: stop automatic task retries, retain separate evidence.

No inference, probes or automatic unblock. Remediation is a triage item, not another
worker on the same broken rail. Unknown exceptions retain the task failure policy.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re


@dataclass(frozen=True)
class InfrastructureCause:
    marker: str
    block_kind: str


def classify_infrastructure(error: str, *, rate_limited=False,
                            terminal_provider=False, infrastructure=False):
    """Use explicit exit classifications first, then narrow runtime signatures.

    A bare number 429, task prose about quota, and ordinary ImportError/MemoryError
    are not evidence of a provider/host problem.
    """
    if terminal_provider:
        return InfrastructureCause("provider_configuration", "capability")
    if rate_limited:
        return InfrastructureCause("provider_quota", "transient")
    if infrastructure:
        return InfrastructureCause("host_scope", "capability")
    missing = re.search(
        r"\b([A-Z][A-Z0-9_]*(?:TOKEN|API_KEY))\s+(?:is\s+)?(?:not set|missing|not configured)\b",
        error,
    )
    if missing:
        return InfrastructureCause("missing_env:" + missing[1], "capability")
    patterns = (
        (r"OpenBLAS\s+(?:error:.*memory allocation|blas_thread_init:.*pthread_create failed)",
         "native_resources", "transient"),
        (r"(?:ImportError|OSError):[^\n]*(?:DLL load failed|WinError (?:126|127|193))",
         "native_loader", "capability"),
        (r"(?:RateLimitError:|(?:Error code:|HTTP(?: status)?)\s*429\b|"
         r"(?:insufficient_quota|usage_limit_reached|quota_exceeded))",
         "provider_quota", "transient"),
    )
    for pattern, marker, kind in patterns:
        if re.search(pattern, error, re.IGNORECASE):
            return InfrastructureCause(marker, kind)
    return None


RUN_START = "HERMES_KANBAN_RUN_START:"


def open_run_log(path, run_id):
    """Bind append-only output to this run before any child can write."""
    stream = open(path, "ab")
    stream.write(f"\n{RUN_START}{run_id}\n".encode("ascii"))
    stream.flush()
    return stream


def dead_worker_cause(dead, task_id, run_id, board=None):
    """Only current-run output is evidence; legacy/unbound logs are ambiguous."""
    explicit = classify_infrastructure("", rate_limited=dead.rate_limited,
                                       terminal_provider=dead.terminal_provider)
    if explicit or dead.code == 0:
        return explicit
    from hermes_cli import kanban_db as kb

    try:
        raw = kb.read_worker_log(task_id, tail_bytes=16000, board=board) or ""
    except (OSError, ValueError):
        return None
    marker = f"{RUN_START}{run_id}\n"
    if marker not in raw:
        return None
    return classify_infrastructure(raw.rsplit(marker, 1)[1])


def held_exit_payload(row, dead):
    """Preserve the dispatch observer contract even though the card is held."""
    return {"task_id": row["id"], "assignee": row["assignee"],
            "run_id": row["current_run_id"], "worker_pid": row["worker_pid"],
            "exit_kind": dead.kind, "exit_code": dead.code,
            "outcome": "infrastructure_blocked", "retry_status": "blocked"}


def park_infrastructure(conn, task_id: str, cause: InfrastructureCause, *,
                        outcome: str, error: str, metadata=None) -> bool:
    """Atomically hold the card, close/tag its exact run, and dedupe remediation.

    Caller owns the write transaction. Neither failure_limit nor the task counter
    changes. All text written here is a fixed marker, never raw subprocess output.
    """
    from hermes_cli import kanban_db as kb

    row = conn.execute(
        "SELECT status, assignee, current_run_id FROM tasks WHERE id=?", (task_id,),
    ).fetchone()
    if row is None or row["status"] not in {"running", "ready", "review"}:
        return False
    profile = row["assignee"] or "unassigned"
    key = "infra-cause:" + hashlib.sha256(
        (profile + "\0" + cause.marker).encode("utf-8")
    ).hexdigest()
    prior = conn.execute(
        "SELECT id, idempotency_key, status FROM tasks WHERE idempotency_key LIKE ? "
        "AND status != 'archived' ORDER BY rowid DESC LIMIT 1", (key + "%",),
    ).fetchone()
    if prior:
        key = key + ":after:" + prior["id"] if prior["status"] == "done" else prior["idempotency_key"]
    remediation = kb.create_task(
        conn, title=f"Infra cause: {profile} / {cause.marker}",
        body=(f"task_type: ops\nInfrastructure hold: {cause.marker}.\n"
              f"First affected task: {task_id}.\n"
              "Action: remove the infrastructure cause, verify a controlled recovery, "
              "then explicitly unblock affected tasks. Do not retry business work or "
              "rotate access without evidence. Archive this signal after resolution "
              "so a future incident can open a new signal. Related tasks are indexed "
              "by infrastructure_blocked events carrying this remediation_task_id."),
        assignee="operations", triage=True, created_by="dispatcher",
        idempotency_key=key,
    )
    detail = {k: v for k, v in (metadata or {}).items()
              if k in {"pid", "exit_kind", "exit_code", "terminal_provider"}}
    detail.update({"failure_class": "infrastructure", "infrastructure": True,
                   "cause_marker": cause.marker, "kind": cause.block_kind,
                   "remediation_task_id": remediation, "trigger_outcome": outcome,
                   "reason": "Infrastructure hold: " + cause.marker,
                   "source_status": kb._retry_status_for_run(conn, task_id)})
    conn.execute(
        "UPDATE tasks SET status='blocked', block_kind=?, claim_lock=NULL, "
        "claim_expires=NULL, worker_pid=NULL, worker_started_at=NULL, "
        "last_failure_error=? WHERE id=?",
        (cause.block_kind, "Infrastructure hold: " + cause.marker, task_id),
    )
    run_id = kb._end_run(conn, task_id, outcome="infrastructure_blocked",
                         status="blocked", error="Infrastructure hold: " + cause.marker,
                         metadata=detail)
    kb._append_event(conn, task_id, "blocked", detail, run_id=run_id)
    kb._append_event(conn, task_id, "infrastructure_blocked", detail, run_id=run_id)
    return True
