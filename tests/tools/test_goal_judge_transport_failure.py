"""Goal-judge transport failures degrade to the canonical review lane.

``judge_goal`` fails open to ``("continue", ..., transport_failed=True)`` when
the auxiliary judge is unreachable. That synthetic verdict must never reject a
finished worker handoff, but it must not mark the task done either. Completion
degrades atomically to the existing review phase, assigned to an independent
profile; no verification child task or second lifecycle is created.

No model or pool changes.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Unit: verdict mapping (tools/kanban_tools.py)
# ---------------------------------------------------------------------------


def test_goal_verdict_none_on_transport_failure():
    """``transport_failed=True`` must map to verdict None (fail-open), never 'continue'."""
    from tools import kanban_tools as kt

    verdict, reason = kt._goal_verdict_and_reason(
        lambda **kw: ("continue", "judge error: PermissionDenied", False, None, True))
    assert verdict is None, (
        f"transport failure must fail open (verdict None), got {verdict!r}")
    assert "transport" in (reason or "").lower()


def test_goal_verdict_passes_through_real_judge_verdicts():
    """A reachable judge's verdicts (incl. synthetic-source 'continue') pass through unchanged."""
    from tools import kanban_tools as kt

    verdict, reason = kt._goal_verdict_and_reason(
        lambda **kw: ("continue", "missing verification evidence", False, None, False))
    assert verdict == "continue"
    assert reason == "missing verification evidence"


# ---------------------------------------------------------------------------
# Handler: kanban_complete fails open on transport failure
# ---------------------------------------------------------------------------


def test_complete_transport_failure_routes_to_review(monkeypatch, tmp_path):
    """Judge transport failure routes completion to the existing review phase."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        goal_task_id = kb.create_task(
            conn, title="goal-mode-test", assignee="test-worker",
            body="Must achieve X with verified evidence.", goal_mode=True
        )
        claimed = kb.claim_task(conn, goal_task_id)
        assert claimed is not None
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", goal_task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))

    monkeypatch.setattr(kt, "_goal_judge_available", lambda: True)
    monkeypatch.setattr(
        kt, "judge_goal",
        lambda **kw: ("continue", "judge error: PermissionDenied", False, None, True))
    monkeypatch.setattr(kt, "_goal_fallback_reviewer", lambda: "company")

    out = kt._handle_complete({
        "summary": "All acceptance criteria verified with evidence.",
        "metadata": {"evidence_refs": ["artifact:report"]},
    })
    payload = json.loads(out)

    assert "error" not in payload, payload
    assert payload["status"] == "review"
    conn2 = kbc.connect()
    try:
        task = kb.get_task(conn2, goal_task_id)
        assert task.status == "review"
        assert task.assignee == "company"
        assert kb.child_ids(conn2, goal_task_id) == []
        run = kb.latest_run(conn2, goal_task_id)
        assert run.outcome == "review_requested"
        assert run.metadata["goal_judge"]["status"] == "transport_failed"
        assert "PermissionDenied" in run.metadata["goal_judge"]["reason"]
        assert run.metadata["receipt"]["status"] == "review_requested"
        events = kb.list_events(conn2, goal_task_id)
        assert events[-1].kind == "review_requested"
        assert events[-1].run_id == run.id
    finally:
        conn2.close()


def test_complete_real_judge_rejection_still_rejects(monkeypatch, tmp_path):
    """A reachable judge's genuine 'continue' still rejects the completion (no bypass)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        goal_task_id = kb.create_task(
            conn, title="goal-mode-test", assignee="test-worker",
            body="Must achieve X with verified evidence.", goal_mode=True
        )
        kb.claim_task(conn, goal_task_id)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", goal_task_id)

    monkeypatch.setattr(kt, "_goal_judge_available", lambda: True)
    monkeypatch.setattr(
        kt, "judge_goal",
        lambda **kw: ("continue", "missing verification evidence", False, None, False))

    out = kt._handle_complete({"summary": "I did some stuff but not X"})
    d = json.loads(out)
    assert "error" in d
    assert "Goal completion rejected by judge" in d["error"]
    assert "missing verification evidence" in d["error"]


# ---------------------------------------------------------------------------
# Canonical review fallback
# ---------------------------------------------------------------------------


def test_fallback_reviewer_skips_worker_self_assignment(monkeypatch):
    from tools import kanban_tools as kt

    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.setattr(kt, "load_config", lambda: {})
    assert kt._goal_fallback_reviewer() == "company"
    assert kt._goal_fallback_reviewer() != "test-worker"


def test_fallback_reviewer_uses_qa_for_company_worker(monkeypatch):
    from tools import kanban_tools as kt

    monkeypatch.setenv("HERMES_PROFILE", "company")
    monkeypatch.setattr(kt, "load_config", lambda: {})
    assert kt._goal_fallback_reviewer() == "qa"


def test_request_review_transport_failure_needs_no_second_lane(monkeypatch):
    from tools import kanban_tools as kt

    task = type("Task", (), {"goal_mode": True, "title": "x", "body": "y"})()
    monkeypatch.setattr(kt, "_goal_judge_available", lambda: True)
    monkeypatch.setattr(
        kt,
        "judge_goal",
        lambda **kw: ("continue", "judge error: PermissionDenied", False, None, True),
    )

    fallback = kt._goal_gate(
        "kanban_request_review", task, "t_parent", "evidence", conn=MagicMock())

    assert "PermissionDenied" in fallback


# ---------------------------------------------------------------------------
# CLI mirror: hermes_cli/kanban.py _goal_mode_handoff_rejection
# ---------------------------------------------------------------------------


def _fake_goal_task():
    task = MagicMock()
    task.goal_mode = True
    task.title = "goal-mode-test"
    task.body = "Must achieve X with verified evidence."
    return task


def test_cli_mirror_transport_failure_routes_to_review(monkeypatch):
    """The CLI mirror exposes transport failure as a review fallback."""
    from hermes_cli import kanban as kcli

    monkeypatch.setattr(
        "agent.auxiliary_client.get_text_auxiliary_client",
        lambda purpose: (MagicMock(), "judge-model"))
    monkeypatch.setattr(
        "hermes_cli.goals.judge_goal",
        lambda **kw: ("continue", "judge error: PermissionDenied", False, None, True))

    verdict, rejection = kcli._goal_mode_handoff_rejection(_fake_goal_task(), "all done")
    assert verdict == "review"
    assert "PermissionDenied" in rejection


def test_cli_complete_transport_failure_routes_original_task_to_review(
    monkeypatch, tmp_path,
):
    from hermes_cli import kanban as kcli
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="cli goal", assignee="test-worker",
            body="Verify the outcome.", goal_mode=True)
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
    monkeypatch.setattr(
        "agent.auxiliary_client.get_text_auxiliary_client",
        lambda purpose: (MagicMock(), "judge-model"))
    monkeypatch.setattr(
        "hermes_cli.goals.judge_goal",
        lambda **kw: ("continue", "judge error: PermissionDenied", False, None, True))
    monkeypatch.setattr(kcli, "_goal_fallback_reviewer", lambda: "company")

    rc = kcli._cmd_complete(SimpleNamespace(
        task_ids=[tid], summary="verified handoff", result=None,
        metadata='{"evidence_refs":["artifact:cli"]}', force=False))

    assert rc == 0
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "review"
        assert task.assignee == "company"
        assert kb.child_ids(conn, tid) == []
        run = kb.latest_run(conn, tid)
        assert run.outcome == "review_requested"
        assert run.metadata["goal_judge"]["status"] == "transport_failed"
        assert run.metadata["receipt"]["status"] == "review_requested"


def test_cli_mirror_real_rejection_still_rejects(monkeypatch):
    """A reachable judge's genuine 'continue' still rejects through the CLI mirror."""
    from hermes_cli import kanban as kcli

    monkeypatch.setattr(
        "agent.auxiliary_client.get_text_auxiliary_client",
        lambda purpose: (MagicMock(), "judge-model"))
    monkeypatch.setattr(
        "hermes_cli.goals.judge_goal",
        lambda **kw: ("continue", "missing evidence", False, None, False))

    verdict, rejection = kcli._goal_mode_handoff_rejection(_fake_goal_task(), "not done")
    assert verdict == "continue"
    assert rejection == "missing evidence"
