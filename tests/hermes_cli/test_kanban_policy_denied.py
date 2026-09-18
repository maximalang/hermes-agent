"""``policy_denied`` is a typed terminal disposition, not a crash.

Fleet Policy projects a deny as ``kanban block --kind policy_denied``. The
Kanban side must:

1. accept the kind and route the task to ``blocked`` (sticky — waits for an
   operator/remediation, never auto-retried by ``recompute_ready``);
2. record ``outcome="policy_denied"`` on the closed run WITH the automatic
   terminal receipt (``receipt.status == "policy_denied"`` + remediation
   next_action) inside the same transaction;
3. leave ``consecutive_failures`` untouched — a deny is not evidence of a
   worker defect and must not consume the crash retry budget;
4. resume from the pre-block phase on unblock.
"""
from __future__ import annotations

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


def _claimed_task(conn, *, title="policy work"):
    tid = kb.create_task(conn, title=title, assignee="worker")
    kb.claim_task(conn, tid, claimer="host:tester")
    return tid


def test_policy_denied_blocks_with_typed_receipt(kanban_home):
    with kbc.connect() as conn:
        tid = _claimed_task(conn)
        assert kb.block_task(
            conn, tid,
            reason="FLEET POLICY BLOCKED [rule_x] remediation: who=operator how=fix precondition",
            kind="policy_denied",
        ) is True

        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_kind == "policy_denied"
        # A deny is not a failure: the crash/timeout breaker budget is intact.
        assert task.consecutive_failures == 0

        run = kb.latest_run(conn, tid)
        assert run.outcome == "policy_denied"
        receipt = run.metadata["receipt"]
        assert receipt["status"] == "policy_denied"
        assert "remediation" in receipt["next_action"]

        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert "blocked" in kinds


def test_policy_denied_is_sticky_for_recompute(kanban_home):
    """recompute_ready must not auto-promote a policy deny: the precondition
    changed only when a human unblocks, which is the no-blind-retry rule."""
    with kbc.connect() as conn:
        tid = _claimed_task(conn)
        kb.block_task(conn, tid, reason="denied", kind="policy_denied")
        promoted = kb.recompute_ready(conn)
        assert promoted == 0
        assert kb.get_task(conn, tid).status == "blocked"


def test_policy_denied_unblock_resumes_phase_without_failure_debt(kanban_home):
    with kbc.connect() as conn:
        tid = _claimed_task(conn)
        kb.block_task(conn, tid, reason="denied", kind="policy_denied")
        assert kb.unblock_task(conn, tid) is True
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 0


def test_plain_block_keeps_blocked_outcome(kanban_home):
    """Regression guard: only ``policy_denied`` gets the typed run outcome;
    generic blocks keep the existing ``blocked`` semantics."""
    with kbc.connect() as conn:
        tid = _claimed_task(conn)
        kb.block_task(conn, tid, reason="waiting", kind="needs_input")
        assert kb.latest_run(conn, tid).outcome == "blocked"
