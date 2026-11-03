"""Respawn guard ``blocker_auth`` freshness binding.

``check_respawn_guard`` matched ``tasks.last_failure_error`` against the
quota/auth regex with no binding to the run that stamped it. A quota-flavored
error stamped by an EARLIER rate-limited requeue survived any later
non-stamping outcome (worker self-block ``blocked``, breaker-adjacent
``gave_up``): the only escapes were a latest outcome of ``rate_limited``
(cooldown elapse) or ``crashed`` (exempt), so the card held on
``blocker_auth`` forever — no cooldown, no failure counting, no breaker trip
(fleet blocker-sweep 03.11: five cards held 12h+ each, "ready queue non-empty,
0 workers spawned"). The hold now releases a stamp that a later ended run
superseded, and keeps a genuinely fresh one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

QUOTA_STAMP = "pid 1 exited rate-limited (quota wall) — requeued"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _seed_ended_run(conn, tid, *, outcome, ended_at, status=None):
    """Claim a fresh run and end it with ``outcome`` at ``ended_at``.

    Lifecycle outcomes end a run WITHOUT stamping ``last_failure_error`` (the
    promote/unblock/assign paths clear it instead), so the task row is only
    reset to requeueable — the stamp is controlled by the test."""
    kb.claim_task(conn, tid)
    run_id = kb.get_task(conn, tid).current_run_id
    conn.execute(
        "UPDATE task_runs SET outcome=?, status=?, ended_at=? WHERE id=?",
        (outcome, status or outcome, ended_at, run_id),
    )
    conn.execute(
        "UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL, "
        "claim_expires=NULL, worker_pid=NULL, worker_started_at=NULL WHERE id=?",
        (tid,),
    )


def _stamp(conn, tid, error):
    conn.execute(
        "UPDATE tasks SET last_failure_error=? WHERE id=?", (error, tid),
    )


def test_stale_quota_stamp_released_after_later_blocked_run(kanban_home, monkeypatch):
    """The fleet shape: a rate-limited requeue stamped the quota error, a later
    worker self-block ended WITHOUT re-stamping — the old text must not park
    the card on ``blocker_auth`` forever."""
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    now = 5_000_000

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="stale stamp", assignee="a")
        _seed_ended_run(conn, tid, outcome="rate_limited", ended_at=now)
        _seed_ended_run(conn, tid, outcome="blocked", ended_at=now + 600)
        _stamp(conn, tid, QUOTA_STAMP)
        conn.commit()

        monkeypatch.setattr(kb.time, "time", lambda: now + 700)
        assert kbd.check_respawn_guard(conn, tid) is None


def test_stale_quota_stamp_released_in_review_lane_after_gave_up(kanban_home, monkeypatch):
    """Same staleness on the review lane with a later ``gave_up`` run: the
    guard falls through to the review-lane release instead of holding."""
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    now = 5_000_000

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="stale stamp review", assignee="a")
        _seed_ended_run(conn, tid, outcome="rate_limited", ended_at=now)
        _seed_ended_run(conn, tid, outcome="gave_up", ended_at=now + 600)
        _stamp(conn, tid, QUOTA_STAMP)
        conn.commit()

        monkeypatch.setattr(kb.time, "time", lambda: now + 700)
        assert kbd.check_respawn_guard(conn, tid, lane="review") is None


def test_fresh_stamp_without_newer_run_still_holds(kanban_home, monkeypatch):
    """Genuinely fresh auth/quota stamps keep the hold: the stamping run IS the
    latest ended run (spawn_failed / timed_out), and a stamp with no ended run
    at all stays held too (fail closed)."""
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    now = 5_000_000

    with kbc.connect() as conn:
        spawn_id = kb.create_task(conn, title="spawn failed", assignee="a")
        _seed_ended_run(conn, spawn_id, outcome="spawn_failed", ended_at=now,
                        status="failed")
        _stamp(conn, spawn_id, "provider authentication failed")

        timeout_id = kb.create_task(conn, title="timed out", assignee="a")
        _seed_ended_run(conn, timeout_id, outcome="timed_out", ended_at=now)
        _stamp(conn, timeout_id, "429 quota exhausted upstream")

        stamp_only_id = kb.create_task(conn, title="stamp only", assignee="a")
        _stamp(conn, stamp_only_id, "401 auth failed")
        conn.commit()

        monkeypatch.setattr(kb.time, "time", lambda: now + 700)
        assert kbd.check_respawn_guard(conn, spawn_id) == "blocker_auth"
        assert kbd.check_respawn_guard(conn, timeout_id) == "blocker_auth"
        assert kbd.check_respawn_guard(conn, stamp_only_id) == "blocker_auth"


def test_rate_limited_cooldown_paths_unchanged(kanban_home, monkeypatch):
    """A latest ``rate_limited`` run still owns the card: cooldown inside,
    probe release after — never a ``blocker_auth`` trap on its own stamp."""
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    now = 5_000_000

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="rl cooldown", assignee="a")
        _seed_ended_run(conn, tid, outcome="rate_limited", ended_at=now)
        _stamp(conn, tid, QUOTA_STAMP)
        conn.commit()

        monkeypatch.setattr(kb.time, "time", lambda: now + 100)
        assert kbd.check_respawn_guard(conn, tid) == "rate_limit_cooldown"
        monkeypatch.setattr(kb.time, "time", lambda: now + 400)
        assert kbd.check_respawn_guard(conn, tid) is None


def test_crashed_latest_run_still_exempt(kanban_home, monkeypatch):
    """A latest ``crashed`` run keeps its exemption: the persisted error is
    worker output (context, not a diagnosis), auth words in it never held."""
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    now = 5_000_000

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="crashed", assignee="a")
        _seed_ended_run(conn, tid, outcome="crashed", ended_at=now, status="failed")
        _stamp(
            conn, tid,
            "pid 1 killed by signal 9. Worker's last output: "
            "'env -u ANTHROPIC_API_KEY claude auth status --text'",
        )
        conn.commit()

        monkeypatch.setattr(kb.time, "time", lambda: now + 700)
        assert kbd.check_respawn_guard(conn, tid) is None
