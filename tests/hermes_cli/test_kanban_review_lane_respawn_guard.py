"""Regression: a stale rate-limit stamp must not park the review lane forever.

Prod defect (observed 2026-10-23, upstream #119070): a worker run hits the quota
wall → the crash sweep requeues the card WITHOUT counting a failure but STAMPS
``tasks.last_failure_error = "pid N exited rate-limited (quota wall) — requeued
without counting a failure"``. Quota returns, the next run SUCCEEDS and hands off
(``kanban_request_review`` → ``task_runs.outcome='review_requested'``,
``status='review'``). ``check_respawn_guard`` then sees: latest outcome is not
``rate_limited`` (no cooldown branch — that only guards a *live* wall), latest
outcome is not ``crashed``, and the stale ``last_failure_error`` still matches
``_RESPAWN_BLOCKER_RE`` ("rate-limited") → returns ``"blocker_auth"`` on the
review lane, which nothing ever clears: the reviewer spawn is held forever.

``complete_task`` clears the stamp on success (``_clear_failure_counter``);
``request_review`` — the other successful terminal transition — did not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB (as in test_kanban_db.py)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


RATE_LIMIT_STAMP = (
    "pid 12788 exited rate-limited (quota wall) — requeued without counting a failure"
)


def _seed_rate_limited_requeue(conn, tid: int, ended_at: int) -> None:
    """First run bounces off the quota wall: the sweep requeues the card without
    counting a failure but stamps the quota-flavored ``last_failure_error``."""
    kb.claim_task(conn, tid)
    run_id = kb.get_task(conn, tid).current_run_id
    conn.execute(
        "UPDATE task_runs SET outcome='rate_limited', status='rate_limited', "
        "ended_at=? WHERE id=?",
        (ended_at, run_id),
    )
    conn.execute(
        "UPDATE tasks SET status='ready', current_run_id=NULL, "
        "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL, "
        "last_failure_error=? WHERE id=?",
        (RATE_LIMIT_STAMP, tid),
    )
    conn.commit()


def _latest_outcome(conn, tid) -> str:
    row = conn.execute(
        "SELECT outcome FROM task_runs WHERE task_id=? AND ended_at IS NOT NULL "
        "ORDER BY ended_at DESC LIMIT 1",
        (tid,),
    ).fetchone()
    return row["outcome"]


def test_rate_limit_stamp_superseded_by_handoff_must_not_guard_review_lane(
    kanban_home, monkeypatch,
):
    """rate_limited requeue (stamp, cf untouched) → successful run →
    request_review: the review lane must spawn the reviewer, not park the card
    as ``blocker_auth`` forever (#119070)."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    now = 5_000_000
    monkeypatch.setattr(_kb.time, "time", lambda: now)

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="rl-then-review", assignee="a")
        _seed_rate_limited_requeue(conn, tid, ended_at=now)

        # Requeue-without-failure semantics: the stamp is there, the counter is not.
        task = kb.get_task(conn, tid)
        assert "rate-limited" in task.last_failure_error
        assert task.consecutive_failures == 0

        # Quota returns: the next run succeeds and hands off for review.
        monkeypatch.setattr(_kb.time, "time", lambda: now + 3600)
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        assert kb.request_review(
            conn, tid, summary="implemented", reviewer="rev", expected_run_id=run_id,
        )
        assert kb.get_task(conn, tid).status == "review"
        assert _latest_outcome(conn, tid) == "review_requested"

        # THE BUG: the stale quota stamp outlived the successful run that
        # superseded it. On clean main this returns "blocker_auth".
        assert kbd.check_respawn_guard(conn, tid, lane="review") is None


def test_active_quota_wall_still_guards_review_lane(kanban_home, monkeypatch):
    """Invariant: while the latest run is ``rate_limited`` INSIDE the cooldown,
    the review lane stays guarded (``rate_limit_cooldown``)."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    now = 5_000_000

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="rl-live-wall", assignee="a")
        _seed_rate_limited_requeue(conn, tid, ended_at=now)
        monkeypatch.setattr(_kb.time, "time", lambda: now + 100)
        assert kbd.check_respawn_guard(conn, tid, lane="review") == "rate_limit_cooldown"


def test_fresh_auth_failure_after_handoff_still_guards_review_lane(
    kanban_home, monkeypatch,
):
    """Invariant: clearing the stamp at the successful handoff is not a
    permanent exemption — a GENUINE failure stamped after the handoff (e.g. the
    PR-acceptance store or a failed reviewer run) must still park the review
    lane as ``blocker_auth``."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    now = 5_000_000
    monkeypatch.setattr(_kb.time, "time", lambda: now)

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="auth-after-handoff", assignee="a")
        _seed_rate_limited_requeue(conn, tid, ended_at=now)
        monkeypatch.setattr(_kb.time, "time", lambda: now + 3600)
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        assert kb.request_review(
            conn, tid, summary="implemented", reviewer="rev", expected_run_id=run_id,
        )

        # A hard auth failure lands on the review card AFTER the handoff.
        monkeypatch.setattr(_kb.time, "time", lambda: now + 7200)
        conn.execute(
            "UPDATE tasks SET last_failure_error=? WHERE id=?",
            ("provider authentication failed", tid),
        )
        conn.commit()
        assert kbd.check_respawn_guard(conn, tid, lane="review") == "blocker_auth"


def test_stale_expected_run_id_leaves_failure_counters_untouched(
    kanban_home, monkeypatch,
):
    """Fence invariant (review follow-up): the counter reset added to fix
    #119070 lives INSIDE the fenced UPDATE (``... AND current_run_id = ?``).
    A ``request_review`` carrying a STALE ``expected_run_id`` — e.g. a zombie
    worker left over from an earlier crashed run — must be rejected wholesale:
    the UPDATE matches no row, the transition does not happen, and the
    accumulated failure state (``consecutive_failures`` /
    ``last_failure_error`` — what the crash breaker and the respawn guard read)
    must stay exactly as it was. A reset leaking past a missed fence would let
    a dead run wipe failure accounting it does not own."""
    import hermes_cli.kanban_db as _kb

    now = 5_000_000
    monkeypatch.setattr(_kb.time, "time", lambda: now)

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="stale-fence", assignee="a")

        # Run 1 crashes: the sweep counts the failure, stamps the error and
        # requeues the card (consecutive_failures=1, card back at 'ready').
        kb.claim_task(conn, tid)
        stale_run_id = kb.get_task(conn, tid).current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome='crashed', status='crashed', ended_at=? "
            "WHERE id=?",
            (now, stale_run_id),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL, "
            "claim_expires=NULL, worker_pid=NULL, consecutive_failures=1, "
            "last_failure_error='pid 424242 exited non-zero' WHERE id=?",
            (tid,),
        )
        conn.commit()

        # Run 2: a fresh worker now owns the card.
        kb.claim_task(conn, tid)
        live_run_id = kb.get_task(conn, tid).current_run_id
        assert live_run_id != stale_run_id
        assert kb.get_task(conn, tid).status == "running"

        # The zombie from run 1 tries to hand off with its stale run id.
        assert kb.request_review(
            conn, tid, summary="zombie handoff", reviewer="rev",
            expected_run_id=stale_run_id,
        ) is False

        # The fence rejected the WHOLE transition — reset included.
        task = kb.get_task(conn, tid)
        assert task.status == "running"  # not 'review'
        assert task.current_run_id == live_run_id  # live claim untouched
        assert task.consecutive_failures == 1  # NOT reset
        assert task.last_failure_error == "pid 424242 exited non-zero"  # NOT cleared
        # No phantom handoff was recorded either.
        assert _latest_outcome(conn, tid) == "crashed"
        assert not conn.execute(
            "SELECT 1 FROM task_events WHERE task_id=? AND kind='review_requested'",
            (tid,),
        ).fetchone()
