"""Owner comment anchors guard both automatic promotion and dispatch."""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'fixture.db'))
    monkeypatch.delenv('HERMES_KANBAN_BOARD', raising=False)
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    kb.init_db()
    with kbc.connect() as connection:
        yield connection


def comment(conn, tid, author, body, when):
    with kb.write_txn(conn):
        return conn.execute(
            'INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)',
            (tid, author, body, when),
        ).lastrowid


def test_anchor_guards_blocked_promotion_and_ready_dispatch(conn, all_assignees_spawnable, caplog):
    tid = kb.create_task(conn, title='fixture', assignee='worker')
    anchor = comment(conn, tid, 'default', 'PARK (audit): leave blocked', 100)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
    kb.recompute_ready(conn)
    assert kb.get_task(conn, tid).status == 'blocked'
    assert tid in caplog.text and str(anchor) in caplog.text
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    result = dispatch.dispatch_once(conn, dry_run=True)
    assert not result.spawned
    assert dict(result.respawn_guarded)[tid]


@pytest.mark.parametrize('author,body,when,allowed', [
    ('company', 'COMPANY RELEASE: conditions met', 101, True),
    ('company', 'COMPANY RELEASE: maintenance hold resolved', 101, True),
    ('default', 'COMPANY RELEASE: conditions met', 101, False),
    ('company', 'note\nCOMPANY RELEASE: conditions met', 101, False),
    ('company', 'COMPANY RELEASE: conditions met', 100, False),
    ('company', 'approved to resume', 101, False),
])
def test_only_later_explicit_company_release(conn, author, body, when, allowed):
    tid = kb.create_task(conn, title='fixture', assignee='worker')
    comment(conn, tid, 'default', 'maintenance HOLD, RR frozen', 100)
    comment(conn, tid, author, body, when)
    assert (dispatch.check_respawn_guard(conn, tid) is None) == allowed


def test_no_anchor_preserves_existing_guard(conn):
    tid = kb.create_task(conn, title='fixture', assignee='worker')
    comment(conn, tid, 'worker', 'PARK', 100)
    assert dispatch.check_respawn_guard(conn, tid) is None


@pytest.mark.parametrize('body', [
    'Original body superseded by this maintenance hold: RR stays frozen.',
    'PARK (аудит 31.10): оставить blocked/scheduled.',
    'COMPANY PARK-ANCHOR: maintenance hold; не поднимать.',
])
def test_incident_anchor_shapes(conn, body):
    tid = kb.create_task(conn, title='fixture', assignee='worker')
    comment(conn, tid, 'default', body, 100)
    assert dispatch.check_respawn_guard(conn, tid) == 'park_anchor'


def test_release_then_new_anchor_guards_again(conn):
    tid = kb.create_task(conn, title='fixture', assignee='worker')
    comment(conn, tid, 'default', 'PARK', 100)
    comment(conn, tid, 'company', 'COMPANY RELEASE: done', 101)
    comment(conn, tid, 'company', 'HOLD', 102)
    assert dispatch.check_respawn_guard(conn, tid) == 'park_anchor'


def test_release_permits_promotion_and_actual_spawn(conn, all_assignees_spawnable):
    tid = kb.create_task(conn, title='fixture', assignee='worker')
    comment(conn, tid, 'default', 'PARK', 100)
    comment(conn, tid, 'company', 'COMPANY RELEASE: done', 101)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
    kb.recompute_ready(conn)
    assert kb.get_task(conn, tid).status == 'ready'
    spawned = []
    dispatch.dispatch_once(conn, spawn_fn=lambda task, workspace, board=None: spawned.append(task.id))
    assert spawned == [tid]


@pytest.mark.parametrize('lane', ['ready', 'review'])
def test_claim_rechecks_under_write_lock(conn, lane):
    tid = kb.create_task(conn, title='fixture', assignee='worker')
    assert dispatch.check_respawn_guard(conn, tid, lane=lane) is None
    comment(conn, tid, 'company', 'HOLD', 100)
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET status=? WHERE id=?', (lane, tid))
    claim = kb.claim_task if lane == 'ready' else kb.claim_review_task
    assert claim(conn, tid, guard_park=True) is None
    assert kb.get_task(conn, tid).status == lane
