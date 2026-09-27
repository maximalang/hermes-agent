"""``lifecycle_metrics``: read-only outcome metrics from the run ledger.

The auto-company control plane must measure itself from durable ledger rows:
receipt coverage, protocol violations, policy denies, median completion time,
retry rate and stuck tasks. Unknown values are ``None`` with a reason — never
fabricated zeros.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _run_cycle(conn, tid, *, outcome, delay=0):
    kb.claim_task(conn, tid, claimer="host:m")
    if delay:
        conn.execute(
            "UPDATE task_runs SET started_at = started_at - ? WHERE task_id = ?",
            (delay, tid),
        )
    return kb._end_run(conn, tid, outcome=outcome, status=outcome)


def _requeue(conn, tid):
    """Simulate the dispatcher releasing a crashed run back to ready."""
    conn.execute(
        "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
        "claim_expires = NULL, current_run_id = NULL WHERE id = ?", (tid,),
    )


def test_metrics_report_receipt_coverage_and_denies(kanban_home):
    with kbc.connect() as conn:
        t1 = kb.create_task(conn, title="done one", assignee="w")
        _run_cycle(conn, t1, outcome="completed", delay=30)
        t2 = kb.create_task(conn, title="denied one", assignee="w")
        kb.claim_task(conn, t2, claimer="host:m")
        kb.block_task(conn, t2, reason="deny", kind="policy_denied")
        t3 = kb.create_task(conn, title="violated", assignee="w")
        kb.claim_task(conn, t3, claimer="host:m")
        kb._end_run(conn, t3, outcome="protocol_violation", status="crashed")
        # retried completion: one failed run then a completed run
        t4 = kb.create_task(conn, title="retried", assignee="w")
        _run_cycle(conn, t4, outcome="crashed")
        _requeue(conn, t4)
        _run_cycle(conn, t4, outcome="completed", delay=10)
        old = kb.create_task(conn, title="stale", assignee="w")
        conn.execute(
            "UPDATE tasks SET created_at = ? WHERE id = ?",
            (int(time.time()) - 40 * 86400, old),
        )

        m = kb.lifecycle_metrics(conn, window_days=28)

    assert m["closed_runs"] == 5
    assert m["receipt_coverage_pct"] == 100.0
    assert m["protocol_violation_pct"] == 20.0
    assert m["policy_denied_pct"] == 20.0
    assert m["completed_tasks"] == 2
    assert m["retry_rate_pct"] == 50.0
    assert m["stuck_tasks"] == 1
    # durations 30 and 10 -> median 20
    assert m["median_claim_to_done_seconds"] == 20
    # unknown cost stays unknown, with the reason visible
    assert m["token_cost_per_accepted_outcome"] is None
    assert "not persisted" in m["token_cost_note"]


def test_metrics_empty_board_reports_none_not_zero(kanban_home):
    with kbc.connect() as conn:
        m = kb.lifecycle_metrics(conn)
    assert m["closed_runs"] == 0
    assert m["receipt_coverage_pct"] is None
    assert m["median_claim_to_done_seconds"] is None
    assert m["retry_rate_pct"] is None


def test_metrics_window_excludes_old_runs(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="old", assignee="w")
        _run_cycle(conn, tid, outcome="completed")
        old_ts = int(time.time()) - 40 * 86400
        conn.execute("UPDATE task_runs SET ended_at = ?", (old_ts,))
        assert kb.lifecycle_metrics(conn, window_days=28)["closed_runs"] == 0
        assert kb.lifecycle_metrics(conn, window_days=60)["closed_runs"] == 1
