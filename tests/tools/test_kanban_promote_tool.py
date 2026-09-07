"""Tests for the kanban_promote tool surface (tools/kanban_tools.py).

Authorization and routing contract (E0):
  * orchestrator-only: a dispatcher-spawned worker (HERMES_KANBAN_TASK set) is
    refused before any DB access, and a delegate_task child is refused too;
  * explicit ``board`` is REQUIRED — promote never rides the env-pinned
    active board;
  * cross-board refusal: when HERMES_KANBAN_DB pins a different board's DB,
    the env pin would silently redirect the write, so the tool refuses;
  * the handler surfaces structured CAS outcomes: ok for applied /
    already_applied / would_apply, tool errors (with live values) for
    conflict / refused / not_found.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest


@pytest.fixture
def orch_env(monkeypatch, tmp_path):
    """Isolated HERMES_HOME, orchestrator profile, no worker/board pins."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-orchestrator")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="stuck card", assignee="worker-a")
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
        conn.commit()
        rev = kb.get_task(conn, tid).revision
    finally:
        conn.close()
    return {"tid": tid, "rev": rev, "home": home}


def _promote(args):
    from tools import kanban_tools as kt
    return json.loads(kt._handle_promote(args))


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------

def test_worker_env_refused_orchestrator_only(monkeypatch, orch_env):
    """A dispatcher-spawned worker cannot promote — the refusal fires before
    any DB access and the card is untouched."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", orch_env["tid"])
    d = _promote({"task_id": orch_env["tid"], "expected_status": "blocked",
                  "expected_revision": orch_env["rev"], "board": "default"})
    assert "error" in d
    assert "orchestrator-only" in d["error"]
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect() as conn:
        assert kb.get_task(conn, orch_env["tid"]).status == "blocked"


def test_delegated_child_refused(monkeypatch, orch_env):
    from agent.delegation_context import delegated_child_context
    with delegated_child_context():
        d = _promote({"task_id": orch_env["tid"], "expected_status": "blocked",
                      "expected_revision": orch_env["rev"], "board": "default"})
    assert "error" in d
    assert "delegate_task child" in d["error"]


def test_tool_hidden_from_worker_schema(monkeypatch, orch_env):
    """The real registry gate: kanban_promote is orchestrator-gated, so a
    worker session (HERMES_KANBAN_TASK set) never sees it, while an
    orchestrator profile with the kanban toolset does."""
    import tools.kanban_tools  # noqa: F401 — ensure registered
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset

    monkeypatch.setenv("HERMES_KANBAN_TASK", orch_env["tid"])
    invalidate_check_fn_cache()
    names = {s["function"].get("name")
             for s in registry.get_definitions(set(resolve_toolset("kanban")), quiet=True)
             if "function" in s}
    assert "kanban_promote" not in names
    assert "kanban_unblock" not in names  # sibling orchestrator tool, same gate

    monkeypatch.delenv("HERMES_KANBAN_TASK")
    monkeypatch.setattr("tools.kanban_tools.load_config",
                        lambda *a, **k: {"toolsets": ["kanban"]})
    invalidate_check_fn_cache()
    names = {s["function"].get("name")
             for s in registry.get_definitions(set(resolve_toolset("kanban")), quiet=True)
             if "function" in s}
    assert "kanban_promote" in names


# ---------------------------------------------------------------------------
# Explicit board + cross-board refusal
# ---------------------------------------------------------------------------

def test_board_argument_required(orch_env):
    d = _promote({"task_id": orch_env["tid"], "expected_status": "blocked",
                  "expected_revision": orch_env["rev"]})
    assert "error" in d
    assert "board is required" in d["error"]


def test_cross_board_db_pin_refused(monkeypatch, orch_env):
    """HERMES_KANBAN_DB pins the DEFAULT board's DB while the call names board
    'alt': the pin would redirect the write to another board, so promote
    refuses instead of silently mutating the pinned DB."""
    from hermes_cli import kanban_db as kb
    pinned = kb.kanban_db_path(board="default")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(pinned))

    # Seed an 'alt' board card so the refusal is provably pre-DB-access.
    from hermes_cli import kanban_db_connect as kbc
    kb.create_board("alt")
    with kbc.connect(board="alt") as conn:
        alt_tid = kb.create_task(conn, title="alt card", assignee="w")
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (alt_tid,))
        conn.commit()
        alt_rev = kb.get_task(conn, alt_tid).revision

    d = _promote({"task_id": alt_tid, "expected_status": "blocked",
                  "expected_revision": alt_rev, "board": "alt"})
    assert "error" in d
    assert "HERMES_KANBAN_DB" in d["error"] and "refused" in d["error"]
    # Neither board mutated.
    with kbc.connect(board="alt") as conn:
        assert kb.get_task(conn, alt_tid).status == "blocked"


def test_matching_db_pin_allowed(monkeypatch, orch_env):
    """A pin that resolves to the named board's own DB is not cross-board."""
    from hermes_cli import kanban_db as kb
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path(board="default")))
    d = _promote({"task_id": orch_env["tid"], "expected_status": "blocked",
                  "expected_revision": orch_env["rev"], "board": "default",
                  "reason": "pinned but matching"})
    assert d.get("ok") is True
    assert d["outcome"] == "applied"


def test_explicit_board_routes_to_named_board(orch_env):
    """board='alt' operates on the alt DB, leaving the default card untouched."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb.create_board("alt")
    with kbc.connect(board="alt") as conn:
        alt_tid = kb.create_task(conn, title="alt stuck", assignee="w")
        conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (alt_tid,))
        conn.commit()
        alt_rev = kb.get_task(conn, alt_tid).revision

    d = _promote({"task_id": alt_tid, "expected_status": "triage",
                  "expected_revision": alt_rev, "board": "alt"})
    assert d.get("ok") is True and d["outcome"] == "applied"
    with kbc.connect(board="alt") as conn:
        assert kb.get_task(conn, alt_tid).status == "ready"
    with kbc.connect() as conn:
        assert kb.get_task(conn, orch_env["tid"]).status == "blocked"

    # The default-board id does not exist on 'alt': not_found, no mutation.
    d2 = _promote({"task_id": orch_env["tid"], "expected_status": "blocked",
                   "expected_revision": orch_env["rev"], "board": "alt"})
    assert "error" in d2
    assert d2["outcome"] == "not_found"


# ---------------------------------------------------------------------------
# Handler outcome mapping + validation
# ---------------------------------------------------------------------------

def test_happy_path_applied(orch_env):
    d = _promote({"task_id": orch_env["tid"], "expected_status": "blocked",
                  "expected_revision": orch_env["rev"], "board": "default",
                  "reason": "recovered after gate stop"})
    assert d["ok"] is True
    assert d["outcome"] == "applied"
    assert d["status"] == "ready"
    assert d["revision"] == orch_env["rev"] + 1
    assert d["previous_status"] == "blocked"
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect() as conn:
        task = kb.get_task(conn, orch_env["tid"])
        assert task.status == "ready"
        audit = [e for e in kb.list_events(conn, orch_env["tid"]) if e.kind == "promoted_cas"]
        assert len(audit) == 1
        assert audit[0].payload["actor"] == "test-orchestrator"
        assert audit[0].payload["reason"] == "recovered after gate stop"


def test_replay_already_applied(orch_env):
    args = {"task_id": orch_env["tid"], "expected_status": "blocked",
            "expected_revision": orch_env["rev"], "board": "default"}
    first = _promote(args)
    assert first["outcome"] == "applied"
    replay = _promote(args)
    assert replay["ok"] is True
    assert replay["outcome"] == "already_applied"


def test_stale_revision_conflict_error(orch_env):
    d = _promote({"task_id": orch_env["tid"], "expected_status": "blocked",
                  "expected_revision": orch_env["rev"] + 7, "board": "default"})
    assert "error" in d
    assert d["outcome"] == "conflict"
    assert d["revision"] == orch_env["rev"]  # live value for the re-read
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect() as conn:
        assert kb.get_task(conn, orch_env["tid"]).status == "blocked"


def test_dry_run_would_apply(orch_env):
    d = _promote({"task_id": orch_env["tid"], "expected_status": "blocked",
                  "expected_revision": orch_env["rev"], "board": "default",
                  "dry_run": True})
    assert d["ok"] is True
    assert d["outcome"] == "would_apply"
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect() as conn:
        assert kb.get_task(conn, orch_env["tid"]).status == "blocked"


def test_missing_and_invalid_args(orch_env):
    base = {"board": "default"}
    d = _promote({**base, "expected_status": "blocked", "expected_revision": 0})
    assert "task_id is required" in d.get("error", "")
    d = _promote({**base, "task_id": orch_env["tid"], "expected_revision": 0})
    assert "expected_status is required" in d.get("error", "")
    d = _promote({**base, "task_id": orch_env["tid"], "expected_status": "blocked"})
    assert "expected_revision must be an integer" in d.get("error", "")
    d = _promote({**base, "task_id": orch_env["tid"], "expected_status": "blocked",
                  "expected_revision": "abc"})
    assert "expected_revision must be an integer" in d.get("error", "")
    # expected_status outside the CAS source set: ValueError -> tool error.
    d = _promote({**base, "task_id": orch_env["tid"], "expected_status": "ready",
                  "expected_revision": 0})
    assert "error" in d and "expected_status" in d["error"]


def test_reason_redacted(orch_env):
    d = _promote({"task_id": orch_env["tid"], "expected_status": "blocked",
                  "expected_revision": orch_env["rev"], "board": "default",
                  "reason": "recovered with token sk-ant-api03-ABCDEFGHIJKLMNOP"})
    assert d["ok"] is True
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect() as conn:
        audit = [e for e in kb.list_events(conn, orch_env["tid"]) if e.kind == "promoted_cas"]
        assert "sk-ant-api03-ABCDEFGHIJKLMNOP" not in json.dumps(audit[0].payload)


# ---------------------------------------------------------------------------
# Race through the handler: exactly one winner, losers get conflict
# ---------------------------------------------------------------------------

def test_concurrent_handler_single_winner(orch_env):
    args = {"task_id": orch_env["tid"], "expected_status": "blocked",
            "expected_revision": orch_env["rev"], "board": "default"}
    results, errors = [], []
    lock = threading.Lock()
    barrier = threading.Barrier(5)

    def attempt():
        try:
            barrier.wait(timeout=10)
            d = _promote(args)
            with lock:
                results.append(d)
        except Exception as exc:  # pragma: no cover
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors

    winners = [d for d in results if d.get("ok") and d.get("outcome") == "applied"]
    replays = [d for d in results if d.get("ok") and d.get("outcome") == "already_applied"]
    conflicts = [d for d in results if "error" in d and d.get("outcome") == "conflict"]
    assert len(winners) == 1, f"exactly one apply expected: {results}"
    assert len(winners) + len(replays) + len(conflicts) == 5
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect() as conn:
        assert kb.get_task(conn, orch_env["tid"]).revision == orch_env["rev"] + 1
        kinds = [e.kind for e in kb.list_events(conn, orch_env["tid"])]
        assert kinds.count("promoted_cas") == 1
