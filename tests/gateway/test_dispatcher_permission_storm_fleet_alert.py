"""Regression tests for the 2026-09-18 stuck-dispatcher outage (t_baed78d9).

A gateway restarted from inside a delegate_task child inherited
``HERMES_DELEGATED_CHILD_CONTEXT`` (carrying the fenced kanban root). Every
dispatcher tick then failed with PermissionError for 40+ minutes and nobody
was notified. Three layers are covered here:

1. gateway boot clears an inherited fence marker (the gateway is the
   dispatcher OWNER, never a delegate child);
2. the per-board dispatcher tracks consecutive PermissionError ticks and
   flags persistent denial;
3. the embedded watcher loop escalates flagged boards into a fleet alert
   (home-channel push + ERROR log) with throttled re-alerts and a
   recovery notice, instead of only logging.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from gateway import kanban_watchers as kw
from gateway.kanban_watchers_dispatcher import _KanbanDispatcher, _resolve_dispatcher_settings

_REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# 1. Gateway boot clears an inherited delegate-child fence
# ---------------------------------------------------------------------------


def test_clear_inherited_delegate_fence_removes_marker(monkeypatch):
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER, clear_inherited_delegate_fence

    monkeypatch.setenv(DELEGATED_CHILD_ENV_MARKER, r"C:\Users\x\AppData\Local\hermes")
    removed = clear_inherited_delegate_fence()
    assert removed == r"C:\Users\x\AppData\Local\hermes"
    assert os.environ.get(DELEGATED_CHILD_ENV_MARKER) is None


def test_clear_inherited_delegate_fence_noop_on_clean_env(monkeypatch):
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER, clear_inherited_delegate_fence

    monkeypatch.delenv(DELEGATED_CHILD_ENV_MARKER, raising=False)
    assert clear_inherited_delegate_fence() is None


def test_run_gateway_boot_clears_poisoned_env(monkeypatch, tmp_path):
    """Boot-order regression: run_gateway() must drop the inherited marker
    BEFORE any kanban code runs, so a gateway restarted from a delegate-child
    lineage dispatches normally instead of failing every tick."""
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER

    poisoned = str(tmp_path / "kanban")
    monkeypatch.setenv(DELEGATED_CHILD_ENV_MARKER, poisoned)

    calls: list[str] = []
    monkeypatch.setattr("hermes_cli.gateway._guard_official_docker_root_gateway",
                        lambda: calls.append("guard0"))
    monkeypatch.setattr("hermes_cli.gateway._guard_named_profile_under_multiplexer",
                        lambda force: calls.append("guard1"))
    monkeypatch.setattr("hermes_cli.gateway._guard_supervised_gateway_conflict",
                        lambda force: calls.append("guard2"))
    monkeypatch.setattr("hermes_cli.gateway._guard_existing_gateway_process_conflict",
                        lambda replace: calls.append("guard3"))
    monkeypatch.setattr("hermes_cli.gateway._apply_startup_watchdog_config", lambda: None)
    monkeypatch.setattr("hermes_cli.gateway._stdin_is_tty", lambda: False)
    monkeypatch.setattr("hermes_cli.gateway._windows_console_window_attached", lambda: False)
    monkeypatch.setattr("hermes_cli.gateway._windows_gateway_breakaway_state", lambda: "none")
    monkeypatch.setattr("hermes_cli.gateway._windows_gateway_should_absorb_console_controls",
                        lambda: False)

    import hermes_cli.gateway as gw

    def _fail_start(**_kwargs):  # pragma: no cover - should never run
        raise AssertionError("start_gateway reached; test stops at boot checks")

    monkeypatch.setattr(gw, "_make_exit_diag", lambda: (lambda *a, **k: None))
    monkeypatch.setattr(gw, "_respawn_storm_backoff", lambda: None)
    monkeypatch.setattr("gateway.run.start_gateway", _fail_start)

    with pytest.raises((AssertionError, OSError, RuntimeError, SystemExit)):
        gw.run_gateway()

    assert os.environ.get(DELEGATED_CHILD_ENV_MARKER) is None, (
        "run_gateway must clear an inherited delegate-child fence at boot"
    )
    # The clear must happen before the guards / any later kanban work.
    assert calls, "run_gateway stopped before its first boot guard; test aborted early"


def test_boot_cleared_env_allows_board_dispatch(monkeypatch, tmp_path):
    """End-to-end incident shape: with the poisoned env present, a ready task
    cannot be claimed (PermissionError); after run_gateway-style clearing the
    same tick dispatches and spawns the worker."""
    home = tmp_path / ".hermes"
    home.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
    # The fenced root is the kanban ROOT (kanban_home()), so pin it to the temp
    # home for the test — production resolves it through the default root.
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = kb.create_task(
            conn, title="p0", assignee="qa", workspace_kind="scratch",
            workspace_path=str(workspace),
        )
    finally:
        conn.close()

    # The fence marker carries the kanban ROOT (the incident's value was
    # C:\Users\max\AppData\Local\hermes — the default hermes root itself), so
    # every board path under it is fenced. Point it at the temp home here.
    poisoned = str(home)
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", poisoned)

    from agent.delegation_context import kanban_path_is_fenced

    assert kanban_path_is_fenced(kb.kanban_db_path()), (
        "precondition: the inherited marker must fence the board before the fix"
    )
    conn = kbc.connect()
    try:
        with pytest.raises(PermissionError):
            kbd.dispatch_once(conn, board=kb.DEFAULT_BOARD)
    finally:
        conn.close()

    # The fix: clear exactly like gateway boot does, dispatch again.
    from agent.delegation_context import clear_inherited_delegate_fence

    clear_inherited_delegate_fence()
    assert not kanban_path_is_fenced(kb.kanban_db_path())

    # QA is not a real profile on this host; stub the spawn so the test stays
    # hermetic and asserts only that the claim+spawn path now REACHES spawning.
    spawned: list = []
    monkeypatch.setattr(kbd, "_default_spawn",
                        lambda task, ws, board=None: spawned.append(task.id) or 4242)
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)

    conn = kbc.connect()
    try:
        result = kbd.dispatch_once(conn, board=kb.DEFAULT_BOARD)
        assert result is not None
        assert [t[0] for t in result.spawned] == [tid]
    finally:
        conn.close()
    assert spawned == [tid]


# ---------------------------------------------------------------------------
# 2. Per-board PermissionError streak tracking
# ---------------------------------------------------------------------------


def _make_dispatcher(tmp_path) -> tuple[_KanbanDispatcher, object]:
    """A dispatcher whose connect() raises PermissionError for *board*."""
    from hermes_cli import kanban_db as kb

    settings = _resolve_dispatcher_settings({}, kb)
    dispatcher = _KanbanDispatcher(kb, settings)
    return dispatcher, kb


def test_permission_error_streaks_accumulate_and_alert(monkeypatch, caplog, tmp_path):
    dispatcher, kb = _make_dispatcher(tmp_path)

    def _raise(_conn):
        raise PermissionError("delegate_task child contexts cannot mutate Kanban tasks or boards")

    import hermes_cli.kanban_db_connect as kbc_mod
    monkeypatch.setattr(kbc_mod, "connect", lambda board=None: (_raise(None) or None))

    dispatcher.tick_once_for_board("fenced")
    assert dispatcher.permission_error_streaks == {"fenced": 1}

    caplog.set_level(logging.ERROR, logger="gateway.run")
    dispatcher.tick_once_for_board("fenced")
    dispatcher.tick_once_for_board("fenced")
    assert dispatcher.permission_error_streaks == {"fenced": 3}
    alerts = [r for r in caplog.records if "[FLEET ALERT]" in r.getMessage()]
    assert len(alerts) == 1, [r.getMessage() for r in caplog.records]
    assert "fenced" in alerts[0].getMessage()


def test_permission_error_streak_resets_on_success(monkeypatch, tmp_path):
    dispatcher, kb = _make_dispatcher(tmp_path)

    state = {"fail": 2}

    def _fake_dispatch_once(_conn, board=None, **_kwargs):
        if state["fail"] > 0:
            state["fail"] -= 1
            raise PermissionError("fenced")
        return object()

    import hermes_cli.kanban_db_dispatch as kbd_mod
    import hermes_cli.kanban_db_connect as kbc_mod
    monkeypatch.setattr(kbc_mod, "connect", lambda board=None: sqlite3.connect(":memory:"))
    monkeypatch.setattr(kbd_mod, "dispatch_once", _fake_dispatch_once)

    dispatcher.tick_once_for_board("b")
    dispatcher.tick_once_for_board("b")
    assert dispatcher.permission_error_streaks == {"b": 2}
    dispatcher.tick_once_for_board("b")  # success
    assert dispatcher.permission_error_streaks == {}


def test_permission_error_streak_resets_on_non_permission_failure(monkeypatch, tmp_path):
    dispatcher, kb = _make_dispatcher(tmp_path)

    import hermes_cli.kanban_db_dispatch as kbd_mod
    import hermes_cli.kanban_db_connect as kbc_mod
    monkeypatch.setattr(kbc_mod, "connect", lambda board=None: sqlite3.connect(":memory:"))
    monkeypatch.setattr(kbd_mod, "dispatch_once",
                        lambda _conn, board=None, **_k: (_ for _ in ()).throw(RuntimeError("boom")))

    dispatcher.permission_error_streaks["b"] = 2
    dispatcher.tick_once_for_board("b")
    assert dispatcher.permission_error_streaks == {}


# ---------------------------------------------------------------------------
# 3. Watcher loop escalation + recovery notice
# ---------------------------------------------------------------------------


class _FakeDispatcher:
    PERMISSION_ERROR_ALERT_THRESHOLD = _KanbanDispatcher.PERMISSION_ERROR_ALERT_THRESHOLD

    def __init__(self, streaks, clear_after=None):
        self.permission_error_streaks = streaks
        self._ticks = 0
        self._clear_after = clear_after

    def tick_once(self):
        self._ticks += 1
        # Simulates the dispatcher clearing a healed streak mid-run (success or
        # recovery), so the recovery notice path can be driven in one run.
        if self._clear_after is not None and self._ticks >= self._clear_after:
            self.permission_error_streaks.clear()
        return []

    def ready_nonempty(self):
        return False

    def auto_decompose_tick(self, _n):
        return 0


def _run_watcher(monkeypatch, runner, ticks_before_stop=2, streaks=None, sent=None,
                 clear_after=None):
    """Run the embedded dispatcher watcher for *ticks_before_stop* real ticks.

    The monkeypatched sleep also serves the watcher's initial 5s boot delay, so
    the stop counter is offset by one sleep call.
    """
    streaks = streaks if streaks is not None else {}
    sent = sent if sent is not None else []

    async def _capture(_self, content):
        sent.append(content)

    monkeypatch.setattr(kw, "_send_fleet_alert_notification", _capture)

    runner._running = True
    monkeypatch.setattr(runner, "_kanban_dispatcher_boot",
                        lambda: (lambda: {}, object(), {}))
    monkeypatch.setattr(kw, "_KanbanDispatcher",
                        lambda _kb, _s: _FakeDispatcher(streaks, clear_after=clear_after))
    monkeypatch.setattr(kw, "_resolve_dispatcher_settings",
                        lambda cfg, kb: type("S", (), {"interval": 1.0})())

    async def _direct(fn, *args):
        return fn(*args)

    sleep_calls = {"n": 0}

    async def _sleep(_delay):
        # Call 1 is the watcher's boot delay; each later call ends a real tick.
        sleep_calls["n"] += 1
        if sleep_calls["n"] > ticks_before_stop:
            runner._running = False

    from hermes_cli import kanban_db_dispatch as kbd

    monkeypatch.setattr(kw, "_to_thread_process_service", _direct)
    monkeypatch.setattr(kw, "_kanban_dispatch_allowed", lambda: True)
    monkeypatch.setattr(kw, "_resolve_auto_decompose_settings", lambda lc: (False, 0))
    monkeypatch.setattr(kbd, "reap_worker_zombies", lambda: [])
    monkeypatch.setattr(kw.asyncio, "sleep", _sleep)

    asyncio.run(asyncio.wait_for(runner._kanban_dispatcher_watcher(), timeout=5.0))
    return sent


def test_watcher_escalates_persistent_permission_error_to_fleet_alert(monkeypatch, caplog):
    runner = object.__new__(kw.GatewayKanbanWatchersMixin)
    streaks = {"fenced": _KanbanDispatcher.PERMISSION_ERROR_ALERT_THRESHOLD}
    sent = _run_watcher(monkeypatch, runner, ticks_before_stop=1, streaks=streaks)

    assert len(sent) == 1
    assert sent[0].startswith("[FLEET ALERT]")
    assert "fenced" in sent[0]
    assert "PermissionError" in sent[0]


def test_watcher_sends_recovery_notice_when_streak_clears(monkeypatch):
    """One run: tick 1 alerts on the fenced board, tick 2 sees the healed
    streak and sends the one-shot recovery notice."""
    runner = object.__new__(kw.GatewayKanbanWatchersMixin)
    streaks = {"fenced": _KanbanDispatcher.PERMISSION_ERROR_ALERT_THRESHOLD}
    sent = _run_watcher(monkeypatch, runner, ticks_before_stop=2, streaks=streaks,
                        clear_after=2)

    assert len(sent) == 2, sent
    assert sent[0].startswith("[FLEET ALERT]")
    assert sent[1].startswith("[FLEET ALERT RESOLVED]")
    assert "fenced" in sent[1]


def test_watcher_does_not_alert_on_transient_single_failure(monkeypatch, caplog):
    runner = object.__new__(kw.GatewayKanbanWatchersMixin)
    sent = _run_watcher(monkeypatch, runner, ticks_before_stop=2, streaks={})
    assert sent == []
