"""Unknown assignee / registry failure must skip dispatch, never spawn-storm.

P1 acceptance: an unknown profile or an unreadable profile registry is a
routing failure detected BEFORE claim/spawn. The task stays ``ready``
(unclaimed, unrun, zero failures) and lands in ``skipped_nonspawnable``;
``_default_spawn`` is never invoked, so no ``hermes -p <ghost>`` crash loop
can start.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_unknown_profile_skips_default_spawn(kanban_home, monkeypatch):
    """A ready card for a nonexistent profile: no claim, no run, no spawn."""
    spawned = []

    def _guard(task, workspace, board=None):
        spawned.append(task.id)
        return None

    monkeypatch.setattr(kbd, "_default_spawn", _guard)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ghost card", assignee="no-such-profile")
        res = kbd.dispatch_once(conn, dry_run=False)
        task = kb.get_task(conn, tid)
        runs = kb.list_runs(conn, tid)
    assert spawned == [], "_default_spawn must never run for an unknown profile"
    assert res.spawned == []
    assert res.skipped_nonspawnable == [tid]
    assert task.status == "ready"
    assert task.claim_lock is None
    assert task.consecutive_failures == 0
    assert runs == []


def test_registry_unavailable_fails_closed_with_default_spawn(kanban_home, monkeypatch):
    """Unreadable profile registry + default spawn: fail closed, card untouched."""
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    spawned = []

    def _default_spawn(task, workspace, board=None):
        spawned.append(task.id)
        return None

    monkeypatch.setattr(kbd, "_default_spawn", _default_spawn)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="registry gone", assignee="worker")
        res = kbd.dispatch_once(conn, dry_run=False)
        task = kb.get_task(conn, tid)
    assert spawned == []
    assert res.skipped_nonspawnable == [tid]
    assert task.status == "ready"
    assert task.consecutive_failures == 0


def test_repeated_ticks_do_not_storm(kanban_home, monkeypatch):
    """Ten ticks over an unknown-profile card produce ten skips, zero spawns."""
    spawn_calls = []
    monkeypatch.setattr(
        kbd, "_default_spawn",
        lambda task, workspace, board=None: spawn_calls.append(task.id),
    )
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="storm probe", assignee="no-such-profile")
        for _ in range(10):
            res = kbd.dispatch_once(conn, dry_run=False)
            assert res.skipped_nonspawnable == [tid]
        task = kb.get_task(conn, tid)
    assert spawn_calls == []
    assert task.status == "ready"
