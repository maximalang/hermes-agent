"""Tests for hermes_cli.kanban_diagnostics — rule-engine that produces
structured distress signals (diagnostics) for kanban tasks.

These tests exercise each rule in isolation using minimal in-memory
task/event/run fixtures (no DB) plus a few integration-style cases
that round-trip through the real kanban_db to make sure the rule
engine works on sqlite3.Row objects as well as dataclasses.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_diagnostics as kd


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _task(**overrides):
    base = {
        "id": "t_demo00",
        "title": "demo task",
        "assignee": "demo",
        "status": "ready",
        "consecutive_failures": 0,
        "last_failure_error": None,
    }
    base.update(overrides)
    return base


def _event(kind, ts=None, **payload):
    return {
        "kind": kind,
        "created_at": int(ts if ts is not None else time.time()),
        "payload": payload or None,
    }


def _run(outcome="completed", run_id=1, error=None):
    return {
        "id": run_id,
        "outcome": outcome,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Each rule — positive + negative + clearing
# ---------------------------------------------------------------------------
















def test_running_with_open_parents_fires_only_while_running():
    """A running card whose parent is not terminal is flagged; the same graph
    on a ready/todo card (the gate is holding it) and a done parent are not."""
    graph = {"parents": [{"id": "t_parent", "title": "p", "status": "todo"}], "children": []}
    diags = kd.compute_task_diagnostics(_task(status="running", started_at=100), [], [], graph=graph)
    assert [d.kind for d in diags] == ["running_with_open_parents"]
    assert diags[0].data["open_parents"] == [{"id": "t_parent", "status": "todo"}]
    assert "hermes kanban unlink t_parent t_demo00" in diags[0].actions[0].payload["command"]
    assert kd.compute_task_diagnostics(_task(status="todo"), [], [], graph=graph) == []
    done_graph = {"parents": [{"id": "t_parent", "title": "p", "status": "done"}], "children": []}
    assert kd.compute_task_diagnostics(_task(status="running"), [], [], graph=done_graph) == []


def test_stuck_in_blocked_fires_past_threshold():
    now = int(time.time())
    task = _task(status="blocked")
    events = [
        _event("blocked", ts=now - 3600 * 48, reason="needs approval"),
    ]
    diags = kd.compute_task_diagnostics(
        task, events, [], now=now,
    )
    assert len(diags) == 1
    d = diags[0]
    assert d.kind == "stuck_in_blocked"
    assert d.severity == "warning"
    assert d.data["age_hours"] >= 48








# ---------------------------------------------------------------------------
# Severity sorting
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Integration — runs through real kanban_db so sqlite.Row fields work
# ---------------------------------------------------------------------------


def test_engine_works_on_sqlite_row_objects(kanban_home):
    """Regression: the rule functions must handle sqlite3.Row (which
    supports mapping access but not attribute access and isn't a dict)
    as well as dataclass Task / plain dict. The API layer passes Row
    objects directly.
    """
    conn = kbc.connect()
    try:
        parent = kb.create_task(conn, title="p", assignee="w")
        real = kb.create_task(conn, title="r", assignee="x", created_by="w")
        with pytest.raises(kb.HallucinatedCardsError):
            kb.complete_task(
                conn, parent,
                summary="with phantom", created_cards=[real, "t_deadbeef1"],
            )
        # Pull Row objects the way the API helper does.
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (parent,),
        ).fetchone()
        events = list(conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id",
            (parent,),
        ).fetchall())
        runs = list(conn.execute(
            "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id",
            (parent,),
        ).fetchall())
        diags = kd.compute_task_diagnostics(row, events, runs)
        assert len(diags) == 1
        assert diags[0].kind == "hallucinated_cards"
        assert "t_deadbeef1" in diags[0].data["phantom_ids"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Error-tolerance: a broken rule shouldn't 500 the whole compute call
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# stranded_in_ready
#
# Surfaces ready tasks that nobody has claimed within the threshold.
# Identity-agnostic by design: catches typo'd assignees, deleted profiles,
# down external worker pools, and misconfigured dispatchers in one rule.
# ---------------------------------------------------------------------------


def test_stranded_in_ready_fires_when_age_exceeds_threshold():
    """Default threshold = 30 min. A ready task promoted 45 min ago
    with no claim should fire as a warning."""
    now = 100_000
    task = _task(status="ready", assignee="demo", claim_lock=None)
    # 45 min = 2700s, threshold = 1800s.
    events = [_event("created", ts=now - 45 * 60)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    stranded = [d for d in diags if d.kind == "stranded_in_ready"]
    assert len(stranded) == 1
    assert stranded[0].severity == "warning"
    assert stranded[0].data["age_seconds"] == 45 * 60
    assert stranded[0].data["assignee"] == "demo"




# ---------------------------------------------------------------------------
# triage_aux_unavailable rule — auto-decompose aware
# ---------------------------------------------------------------------------


def _triage_task():
    return _task(id="t_triage1", status="triage")








def test_severity_at_or_above_uses_threshold_semantics():
    assert kd.severity_at_or_above("warning", "warning") is True
    assert kd.severity_at_or_above("error", "warning") is True
    assert kd.severity_at_or_above("critical", "warning") is True
    assert kd.severity_at_or_above("critical", "error") is True
    assert kd.severity_at_or_above("warning", "error") is False
    assert kd.severity_at_or_above("error", "critical") is False
    assert kd.severity_at_or_above("mystery", "warning") is False
    assert kd.severity_at_or_above("warning", None) is True


# ---------------------------------------------------------------------------
# reclaim_defer_exhausted / reap_refused — defer budget + reap markers (t_4fcd3e44)
# ---------------------------------------------------------------------------


def test_reclaim_defer_rule_fires_on_simulated_series_over_threshold():
    now = int(time.time())
    task = _task(status="running", current_run_id=7)
    events = [_event("claimed", ts=now - 4000)]
    events += [_event("reclaim_deferred", ts=now - 4000 + i * 120,
                      reason="ttl_expired_worker_alive",
                      refusal_reason="unverified_worker_fingerprint", prev_pid=9191)
               for i in range(30)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    fired = [d for d in diags if d.kind == "reclaim_defer_exhausted"]
    assert fired and fired[0].severity == "error"
    assert fired[0].data["deferred_count"] == 30
    assert fired[0].data["refusal_reason"] == "unverified_worker_fingerprint"
    assert any(a.kind == "reclaim" for a in fired[0].actions)


def test_reclaim_defer_rule_deadline_trips_below_threshold():
    now = int(time.time())
    task = _task(status="running", current_run_id=7)
    events = [_event("reclaim_deferred", ts=now - 3 * 3600, reason="ttl_expired_worker_alive"),
              _event("reclaim_deferred", ts=now - 7200, reason="ttl_expired_worker_alive"),
              _event("reclaim_deferred", ts=now - 60, reason="ttl_expired_worker_alive")]
    assert any(d.kind == "reclaim_defer_exhausted"
               for d in kd.compute_task_diagnostics(task, events, [], now=now))


def test_reclaim_defer_rule_silent_inside_budget_and_off_running():
    now = int(time.time())
    task = _task(status="running", current_run_id=7)
    events = [_event("reclaim_deferred", ts=now - i * 120, reason="ttl_expired_worker_alive")
              for i in range(5)]
    assert not [d for d in kd.compute_task_diagnostics(task, events, [], now=now)
                if d.kind == "reclaim_defer_exhausted"]
    done = _task(status="done", current_run_id=7)
    long_series = [_event("reclaim_deferred", ts=now - i * 120) for i in range(40)]
    assert not [d for d in kd.compute_task_diagnostics(done, long_series, [], now=now)
                if d.kind == "reclaim_defer_exhausted"]


def test_reclaim_defer_rule_marker_is_fast_path():
    now = int(time.time())
    task = _task(status="running", current_run_id=7)
    events = [
        _event("reclaim_deferred", ts=now - 4000, reason="ttl_expired_worker_alive"),
        _event("reclaim_defer_budget_exhausted", ts=now - 100, deferred_count=25,
               first_deferred_at=now - 4000, refusal_reason="unverified_worker_fingerprint"),
    ]
    fired = [d for d in kd.compute_task_diagnostics(task, events, [], now=now)
             if d.kind == "reclaim_defer_exhausted"]
    assert fired and fired[0].data["budget_marker"] is True
    assert fired[0].data["deferred_count"] == 25


def test_reclaim_defer_rule_series_broken_by_lifecycle_event():
    now = int(time.time())
    task = _task(status="running", current_run_id=7)
    events = ([_event("reclaim_deferred", ts=now - 5000 + i * 120) for i in range(30)]
              + [_event("reclaimed", ts=now - 1000)]
              + [_event("reclaim_deferred", ts=now - i * 120) for i in range(3)])
    assert not [d for d in kd.compute_task_diagnostics(task, events, [], now=now)
                if d.kind == "reclaim_defer_exhausted"]


def test_reap_refused_rule_fires_when_attempt_budget_spent():
    now = int(time.time())
    task = _task(status="done", current_run_id=3)
    events = [
        _event("bound_reap_refused", ts=now - 3600, refusal_reason="claim_lock_not_local",
               pid=9191, worker_started_at="unverified", attempt=1),
        _event("bound_reap_refused", ts=now - 2400, refusal_reason="claim_lock_not_local",
               pid=9191, worker_started_at="unverified", attempt=2),
        _event("bound_reap_refused", ts=now - 1200, refusal_reason="claim_lock_not_local",
               pid=9191, worker_started_at="unverified", attempt=3),
    ]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    fired = [d for d in diags if d.kind == "reap_refused"]
    assert fired and fired[0].data["attempts"] == 3
    assert fired[0].data["refusal_reason"] == "claim_lock_not_local"
    assert fired[0].data["pid_reuse_suspected"] is False


def test_reap_refused_rule_worst_case_pid_reuse():
    now = int(time.time())
    task = _task(status="done", current_run_id=3)
    events = [_event("bound_reap_refused", ts=now - i * 600,
                     refusal_reason="unverified_pid_in_use",
                     pid=4242, worker_started_at="unverified", attempt=4 - i)
              for i in (2, 1, 0)]
    fired = [d for d in kd.compute_task_diagnostics(task, events, [], now=now)
             if d.kind == "reap_refused"]
    assert fired and fired[0].data["pid_reuse_suspected"] is True
    assert fired[0].data["pid"] == 4242


def test_reap_refused_rule_silent_inside_budget_and_after_reaping():
    now = int(time.time())
    task = _task(status="done", current_run_id=3)
    two = [_event("bound_reap_refused", ts=now - 600, refusal_reason="claim_lock_not_local",
                  pid=9191, worker_started_at="unverified", attempt=2),
           _event("bound_reap_refused", ts=now - 300, refusal_reason="claim_lock_not_local",
                  pid=9191, worker_started_at="unverified", attempt=1)]
    assert not [d for d in kd.compute_task_diagnostics(task, two, [], now=now)
                if d.kind == "reap_refused"]
    resolved = two + [_event("terminal_worker_reaped", ts=now - 60, pid=9191,
                             worker_started_at="unverified", terminated=True)]
    assert not [d for d in kd.compute_task_diagnostics(task, resolved, [], now=now)
                if d.kind == "reap_refused"]
