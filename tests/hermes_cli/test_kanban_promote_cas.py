"""Tests for promote_task_cas — the atomic, identity-preserving recovery CAS.

Design contract (E0):
  * ``tasks.revision`` starts at 0 and auto-bumps whenever an UPDATE lands a
    new status (trigger ``tg_tasks_bump_revision``); legacy DBs receive the
    column + trigger through the additive migration pass.
  * ``promote_task_cas`` mutates only when live status/revision (and, when
    given, the run pointer) match what the caller read. A stale read is a
    ``conflict`` with ZERO mutation — no status change, no revision bump, no
    event row.
  * Replaying a successful promotion reports ``already_applied`` (idempotent).
  * Dependency gating: a card with unfinished parents lands in ``todo``, never
    ``ready``.
  * Identity/history survive: same row, same id/created_at, comments, runs and
    events untouched (only an appended ``promoted_cas`` audit event).
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as c:
        yield c


def _set_status(conn, tid, status):
    """Direct-SQL setup helper (bypasses lifecycle validation on purpose)."""
    conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
    conn.commit()


def _row(conn, tid):
    return conn.execute(
        "SELECT status, revision, current_run_id FROM tasks WHERE id=?", (tid,)
    ).fetchone()


def _cas(conn, tid, **kw):
    kw.setdefault("actor", "tester")
    return kb.promote_task_cas(conn, tid, **kw)


# ---------------------------------------------------------------------------
# Schema / migration / trigger
# ---------------------------------------------------------------------------

def test_fresh_schema_has_revision_default_zero_and_trigger(conn):
    cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(tasks)")}
    assert "revision" in cols
    assert cols["revision"]["dflt_value"] is not None and "0" in str(cols["revision"]["dflt_value"])
    assert cols["revision"]["notnull"] == 1
    trig = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name='tg_tasks_bump_revision'"
    ).fetchone()
    assert trig is not None


def test_new_task_starts_at_revision_zero(conn):
    tid = kb.create_task(conn, title="fresh", assignee="w")
    assert kb.get_task(conn, tid).revision == 0


def test_trigger_bumps_only_on_status_change(conn):
    tid = kb.create_task(conn, title="bump", assignee="w")
    assert kb.get_task(conn, tid).revision == 0
    # Non-status update: no bump.
    conn.execute("UPDATE tasks SET priority=42 WHERE id=?", (tid,))
    conn.commit()
    assert kb.get_task(conn, tid).revision == 0
    # Status update: bump.
    _set_status(conn, tid, "blocked")
    assert kb.get_task(conn, tid).revision == 1
    # Setting the SAME status again: no bump (WHEN NEW.status IS NOT OLD.status).
    _set_status(conn, tid, "blocked")
    assert kb.get_task(conn, tid).revision == 1
    _set_status(conn, tid, "ready")
    assert kb.get_task(conn, tid).revision == 2


def test_legacy_db_migration_adds_revision_and_trigger(tmp_path, monkeypatch):
    """A board predating the revision column migrates cleanly on connect:
    column added with default 0, trigger created after the column exists,
    existing rows readable at revision 0."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    db_path = home / "kanban.db"
    raw = sqlite3.connect(str(db_path))
    # Pre-revision ``tasks`` shape (subset of the v1 schema, like the legacy
    # test in test_kanban_db.py): no revision column, no trigger.
    raw.execute("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT,
            assignee TEXT,
            status TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 0,
            created_by TEXT,
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            completed_at INTEGER,
            workspace_kind TEXT NOT NULL DEFAULT 'scratch',
            workspace_path TEXT,
            claim_lock TEXT,
            claim_expires INTEGER
        )
    """)
    raw.execute(
        "INSERT INTO tasks (id, title, status, created_at) "
        "VALUES ('legacy-1', 'old card', 'blocked', 1)"
    )
    raw.commit()
    raw.close()

    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kbc.connect(db_path) as migrated:
        cols = {r["name"] for r in migrated.execute("PRAGMA table_info(tasks)")}
        assert "revision" in cols
        trig = migrated.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name='tg_tasks_bump_revision'"
        ).fetchone()
        assert trig is not None
        task = kb.get_task(migrated, "legacy-1")
        assert task is not None and task.revision == 0
        # The migrated legacy row participates in CAS like any fresh row.
        res = _cas(migrated, "legacy-1", expected_status="blocked", expected_revision=0)
        assert res["outcome"] == "applied"
        assert res["status"] == "ready"
        assert kb.get_task(migrated, "legacy-1").revision == 1


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

def test_apply_blocked_to_ready_preserves_identity_and_history(conn):
    tid = kb.create_task(conn, title="stuck", assignee="worker-a", body="spec text")
    kb.add_comment(conn, tid, "operator", "context note")
    _set_status(conn, tid, "blocked")
    before = kb.get_task(conn, tid)
    comments_before = kb.list_comments(conn, tid)
    events_before = kb.list_events(conn, tid)

    res = _cas(conn, tid, expected_status="blocked", expected_revision=before.revision,
               reason="operator recovered the card")
    assert res["outcome"] == "applied"
    assert res["status"] == "ready"
    assert res["previous_status"] == "blocked"
    assert res["revision"] == before.revision + 1

    after = kb.get_task(conn, tid)
    # Identity/history preserved: same row, same core fields.
    assert after.id == before.id
    assert after.created_at == before.created_at
    assert after.title == before.title and after.body == before.body
    assert after.assignee == before.assignee
    assert after.status == "ready"
    assert after.revision == before.revision + 1
    assert [c.body for c in kb.list_comments(conn, tid)] == [c.body for c in comments_before]
    # Prior events untouched; exactly one audit event appended.
    after_events = kb.list_events(conn, tid)
    assert len(after_events) == len(events_before) + 1
    audit = after_events[-1]
    assert audit.kind == "promoted_cas"
    assert audit.payload["actor"] == "tester"
    assert audit.payload["reason"] == "operator recovered the card"
    assert audit.payload["from_status"] == "blocked"
    assert audit.payload["to_status"] == "ready"
    assert audit.payload["expected_revision"] == before.revision
    assert audit.payload["new_revision"] == before.revision + 1


def test_apply_triage_and_todo_sources(conn):
    for source in ("triage", "todo"):
        tid = kb.create_task(conn, title=f"src-{source}", assignee="w")
        _set_status(conn, tid, source)
        rev = kb.get_task(conn, tid).revision
        res = _cas(conn, tid, expected_status=source, expected_revision=rev)
        assert res["outcome"] == "applied", f"{source}: {res}"
        assert res["status"] == "ready"


def test_idempotent_replay_reports_already_applied(conn):
    tid = kb.create_task(conn, title="replay", assignee="w")
    _set_status(conn, tid, "blocked")
    rev = kb.get_task(conn, tid).revision
    first = _cas(conn, tid, expected_status="blocked", expected_revision=rev)
    assert first["outcome"] == "applied"
    replay = _cas(conn, tid, expected_status="blocked", expected_revision=rev)
    assert replay["outcome"] == "already_applied"
    assert replay["status"] == "ready"
    # No extra mutation or event from the replay.
    assert kb.get_task(conn, tid).revision == first["revision"]
    kinds = [e.kind for e in kb.list_events(conn, tid)]
    assert kinds.count("promoted_cas") == 1


def test_two_revision_stale_replay_conflicts(conn):
    """The original P0 reproduction: apply blocked rev3 -> ready rev4, then a
    caller holding an OLDER read (rev2) must NOT see already_applied — the
    card is past that intent by more than one bump."""
    tid = kb.create_task(conn, title="stale-replay", assignee="w")
    _set_status(conn, tid, "blocked")   # rev1
    _set_status(conn, tid, "ready")     # rev2 — another writer moved it on
    _set_status(conn, tid, "blocked")   # rev3
    rev3 = kb.get_task(conn, tid).revision
    assert rev3 == 3
    applied = _cas(conn, tid, expected_status="blocked", expected_revision=rev3)
    assert applied["outcome"] == "applied"
    assert applied["revision"] == rev3 + 1
    # Stale caller read rev2 (or anything != rev3): conflict, zero mutation.
    for stale_rev in (rev3 - 1, rev3 + 5, 0):
        res = _cas(conn, tid, expected_status="blocked", expected_revision=stale_rev)
        assert res["outcome"] == "conflict", f"rev {stale_rev}: {res}"
        assert "stale promotion replay" in res["error"]
        assert res["revision"] == rev3 + 1  # live values for the re-read
        _assert_untouched(conn, tid, "ready", rev3 + 1, cas_events=1)


def test_post_claim_replay_conflicts(conn):
    """After the dispatcher claims the promoted card (ready -> running, fresh
    run pointer), the exact replay must conflict: the card moved on and a
    re-apply must never be mistaken for idempotent success."""
    tid = kb.create_task(conn, title="post-claim", assignee="w")
    _set_status(conn, tid, "blocked")
    rev = kb.get_task(conn, tid).revision
    first = _cas(conn, tid, expected_status="blocked", expected_revision=rev)
    assert first["outcome"] == "applied"
    kb.claim_task(conn, tid)
    task = kb.get_task(conn, tid)
    assert task.status == "running" and task.current_run_id is not None
    replay = _cas(conn, tid, expected_status="blocked", expected_revision=rev)
    assert replay["outcome"] == "conflict"
    assert "stale promotion replay" in replay["error"]
    assert replay["revision"] == task.revision
    assert replay["current_run_id"] == task.current_run_id
    # Zero mutation: still running under the same run, one audit event.
    _assert_untouched(conn, tid, "running", task.revision, cas_events=1)
    assert kb.get_task(conn, tid).current_run_id == task.current_run_id


def test_post_block_reopen_replay_conflicts(conn):
    """Promote -> block -> unblock cycles the card back to a source status at
    a higher revision: the old replay is stale (conflict), while a FRESH CAS
    on the live read still applies. Same row, history preserved."""
    tid = kb.create_task(conn, title="cycle", assignee="w")
    kb.add_comment(conn, tid, "operator", "before cycle")
    _set_status(conn, tid, "blocked")
    rev = kb.get_task(conn, tid).revision
    first = _cas(conn, tid, expected_status="blocked", expected_revision=rev)
    assert first["outcome"] == "applied"
    kb.block_task(conn, tid, reason="hit a wall", kind="needs_input")
    live_rev = kb.get_task(conn, tid).revision
    assert live_rev > rev + 1  # block bumped further
    replay = _cas(conn, tid, expected_status="blocked", expected_revision=rev)
    assert replay["outcome"] == "conflict"
    # The card cycled back to a source status, so the ordinary revision guard
    # catches the stale read (live rev moved past the caller's).
    assert "revision mismatch" in replay["error"]
    _assert_untouched(conn, tid, "blocked", live_rev, cas_events=1)
    # A fresh read re-applies: second promoted_cas event, same task id.
    second = _cas(conn, tid, expected_status="blocked", expected_revision=live_rev)
    assert second["outcome"] == "applied"
    assert second["status"] == "ready"
    assert second["revision"] == live_rev + 1
    assert kb.get_task(conn, tid).id == tid
    assert [c.body for c in kb.list_comments(conn, tid)] == ["before cycle"]
    kinds = [e.kind for e in kb.list_events(conn, tid)]
    assert kinds.count("promoted_cas") == 2


def test_dependency_gating_lands_todo_while_parent_unfinished(conn):
    parent = kb.create_task(conn, title="parent", assignee="w")
    child = kb.create_task(conn, title="child", assignee="w", parents=[parent])
    _set_status(conn, child, "blocked")
    rev = kb.get_task(conn, child).revision
    res = _cas(conn, child, expected_status="blocked", expected_revision=rev)
    assert res["outcome"] == "applied"
    assert res["status"] == "todo"  # gated: parent not done
    # After the parent completes, recompute_ready flips it to ready.
    kb.complete_task(conn, parent, result="done")
    kb.recompute_ready(conn)
    assert kb.get_task(conn, child).status == "ready"


def test_todo_with_unfinished_parent_is_refused_as_noop(conn):
    parent = kb.create_task(conn, title="p", assignee="w")
    child = kb.create_task(conn, title="c", assignee="w", parents=[parent])
    assert kb.get_task(conn, child).status == "todo"
    rev = kb.get_task(conn, child).revision
    res = _cas(conn, child, expected_status="todo", expected_revision=rev)
    assert res["outcome"] == "refused"
    assert "no-op" in res["error"]
    assert kb.get_task(conn, child).revision == rev


def test_dry_run_validates_without_writing(conn):
    tid = kb.create_task(conn, title="dry", assignee="w")
    _set_status(conn, tid, "blocked")
    rev = kb.get_task(conn, tid).revision
    res = _cas(conn, tid, expected_status="blocked", expected_revision=rev, dry_run=True)
    assert res["outcome"] == "would_apply"
    assert res["status"] == "ready"
    assert kb.get_task(conn, tid).status == "blocked"
    assert kb.get_task(conn, tid).revision == rev
    assert not [e for e in kb.list_events(conn, tid) if e.kind == "promoted_cas"]
    # The validated CAS still applies afterwards.
    applied = _cas(conn, tid, expected_status="blocked", expected_revision=rev)
    assert applied["outcome"] == "applied"


# ---------------------------------------------------------------------------
# Negative paths — stale reads mutate NOTHING
# ---------------------------------------------------------------------------

def _assert_untouched(conn, tid, status, revision, cas_events=0):
    row = _row(conn, tid)
    assert row["status"] == status
    assert row["revision"] == revision
    kinds = [e.kind for e in kb.list_events(conn, tid)]
    assert kinds.count("promoted_cas") == cas_events


def test_stale_revision_conflict_mutates_nothing(conn):
    tid = kb.create_task(conn, title="stale", assignee="w")
    _set_status(conn, tid, "blocked")
    live_rev = kb.get_task(conn, tid).revision
    # Another writer cycles the card (blocked -> todo -> blocked): the status
    # reads the same again, but the revision moved on — exactly the stale-read
    # case the CAS must catch on the revision guard alone.
    _set_status(conn, tid, "todo")
    _set_status(conn, tid, "blocked")
    new_rev = kb.get_task(conn, tid).revision
    assert new_rev == live_rev + 2
    res = _cas(conn, tid, expected_status="blocked", expected_revision=live_rev)
    assert res["outcome"] == "conflict"
    assert "revision mismatch" in res["error"]
    assert res["revision"] == new_rev  # live values returned for the re-read
    _assert_untouched(conn, tid, "blocked", new_rev)


def test_expected_status_mismatch_conflicts(conn):
    tid = kb.create_task(conn, title="wrong-source", assignee="w")
    _set_status(conn, tid, "blocked")
    rev = kb.get_task(conn, tid).revision
    res = _cas(conn, tid, expected_status="todo", expected_revision=rev)
    assert res["outcome"] == "conflict"
    assert "status mismatch" in res["error"]
    _assert_untouched(conn, tid, "blocked", rev)


def test_run_pointer_mismatch_conflicts(conn):
    tid = kb.create_task(conn, title="run-guard", assignee="w")
    _set_status(conn, tid, "blocked")
    rev = kb.get_task(conn, tid).revision
    res = _cas(conn, tid, expected_status="blocked", expected_revision=rev,
               expected_current_run_id=999)
    assert res["outcome"] == "conflict"
    assert "current_run_id mismatch" in res["error"]
    _assert_untouched(conn, tid, "blocked", rev)


def test_stale_run_pointer_refused_without_ack(conn):
    """A card still pointing at a run is refused at the real mutation boundary
    unless the caller acknowledges the exact pointer (and consumes it)."""
    tid = kb.create_task(conn, title="stale-run", assignee="w")
    kb.claim_task(conn, tid)
    run_id = kb.get_task(conn, tid).current_run_id
    assert run_id is not None
    _set_status(conn, tid, "blocked")
    rev = kb.get_task(conn, tid).revision
    # Re-point the run at the blocked row like a leaked worker would.
    conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, tid))
    conn.commit()

    res = _cas(conn, tid, expected_status="blocked", expected_revision=rev)
    assert res["outcome"] == "refused"
    assert "stale-run" in res["error"]
    _assert_untouched(conn, tid, "blocked", rev)
    assert kb.get_task(conn, tid).current_run_id == run_id

    # Acknowledging the exact pointer applies and consumes it.
    res2 = _cas(conn, tid, expected_status="blocked", expected_revision=rev,
                expected_current_run_id=run_id)
    assert res2["outcome"] == "applied"
    assert kb.get_task(conn, tid).current_run_id is None
    # The leaked run row was closed (invariant: NULL pointer <=> terminal run).
    run = kb.get_run(conn, run_id)
    assert run is not None and run.ended_at is not None


def test_terminal_statuses_refused(conn):
    for status in ("done", "archived", "review", "scheduled"):
        tid = kb.create_task(conn, title=f"term-{status}", assignee="w")
        _set_status(conn, tid, status)
        rev = kb.get_task(conn, tid).revision
        res = _cas(conn, tid, expected_status="blocked", expected_revision=rev)
        assert res["outcome"] == "refused", f"{status}: {res}"
        assert res["status"] == status
        _assert_untouched(conn, tid, status, rev)


def test_not_found(conn):
    res = _cas(conn, "t_doesnotexist", expected_status="blocked", expected_revision=0)
    assert res["outcome"] == "not_found"


def test_invalid_expected_status_raises(conn):
    tid = kb.create_task(conn, title="validate", assignee="w")
    with pytest.raises(ValueError):
        _cas(conn, tid, expected_status="ready", expected_revision=0)
    with pytest.raises(ValueError):
        _cas(conn, tid, expected_status="done", expected_revision=0)


def test_non_int_expected_revision_raises(conn):
    tid = kb.create_task(conn, title="validate2", assignee="w")
    with pytest.raises(ValueError):
        _cas(conn, tid, expected_status="todo", expected_revision="0")
    with pytest.raises(ValueError):
        _cas(conn, tid, expected_status="todo", expected_revision=True)


# ---------------------------------------------------------------------------
# Race: concurrent CAS attempts on one card — exactly one winner
# ---------------------------------------------------------------------------

def test_concurrent_cas_single_winner(kanban_home):
    tid = None
    with kbc.connect() as setup:
        tid = kb.create_task(setup, title="race", assignee="w")
        _set_status(setup, tid, "blocked")
    rev = None
    with kbc.connect() as setup:
        rev = kb.get_task(setup, tid).revision

    results: list = []
    errors: list = []
    lock = threading.Lock()
    barrier = threading.Barrier(6)

    def attempt():
        try:
            barrier.wait(timeout=10)
            with kbc.connect() as c:
                res = _cas(c, tid, expected_status="blocked", expected_revision=rev)
            with lock:
                results.append(res)
        except Exception as exc:  # pragma: no cover — surfaced via assertion
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"race threads raised: {errors}"
    outcomes = [r["outcome"] for r in results]
    assert outcomes.count("applied") == 1, f"expected exactly one winner: {outcomes}"
    assert all(o in ("applied", "conflict", "already_applied") for o in outcomes), outcomes
    # Exactly one audit event; revision bumped exactly once by the winner.
    with kbc.connect() as c:
        assert kb.get_task(c, tid).revision == rev + 1
        kinds = [e.kind for e in kb.list_events(c, tid)]
        assert kinds.count("promoted_cas") == 1
