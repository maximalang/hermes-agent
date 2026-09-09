"""Batch 0.3-P2a: dispatcher-issued principal admission only.

No test connects RuntimePrincipal to a Kanban business write.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_principal as kp


@pytest.fixture(autouse=True)
def reset_principal():
    kp._clear_current_principal_for_tests()
    yield
    kp._clear_current_principal_for_tests()


@pytest.fixture
def active_run(tmp_path):
    db_path = tmp_path / "kanban.db"
    conn = kbc.connect(db_path=db_path)
    task_id = kb.create_task(conn, title="principal admission", assignee="qa")
    claimed = kb.claim_task(conn, task_id, claimer="test-host:100")
    assert claimed is not None and claimed.current_run_id is not None and claimed.claim_lock
    binding = {
        "board": "default",
        "db_path": db_path,
        "task_id": task_id,
        "run_id": int(claimed.current_run_id),
        "profile": "qa",
        "claim_lock": claimed.claim_lock,
    }
    try:
        yield conn, binding
    finally:
        conn.close()


def issue(conn, binding, **kwargs):
    return kp.issue_local_launch(
        conn,
        board=binding["board"],
        task_id=binding["task_id"],
        run_id=binding["run_id"],
        expected_profile=binding["profile"],
        claim_lock=binding["claim_lock"],
        **kwargs,
    )


def capture(monkeypatch, binding, raw=None, **overrides):
    values = {
        kp._LAUNCH_CAPABILITY_ENV: raw,
        "HERMES_KANBAN_DB": str(binding["db_path"]),
        "HERMES_KANBAN_BOARD": binding["board"],
        "HERMES_KANBAN_TASK": binding["task_id"],
        "HERMES_KANBAN_RUN_ID": str(binding["run_id"]),
        "HERMES_PROFILE": binding["profile"],
        "HERMES_KANBAN_CLAIM_LOCK": binding["claim_lock"],
    }
    values.update(overrides)
    for key, value in values.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, str(value))
    return kp.capture_startup_capability()


def event_rows(conn, kind):
    return conn.execute(
        "SELECT id, task_id, run_id, payload FROM task_events WHERE kind=? ORDER BY id",
        (kind,),
    ).fetchall()


def child_env(binding, raw):
    env = os.environ.copy()
    env.update({
        kp._LAUNCH_CAPABILITY_ENV: raw,
        "HERMES_KANBAN_DB": str(binding["db_path"]),
        "HERMES_KANBAN_BOARD": binding["board"],
        "HERMES_KANBAN_TASK": binding["task_id"],
        "HERMES_KANBAN_RUN_ID": str(binding["run_id"]),
        "HERMES_PROFILE": binding["profile"],
        "HERMES_KANBAN_CLAIM_LOCK": binding["claim_lock"],
    })
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    return env


def run_child(binding, raw):
    helper = Path(__file__).with_name("_principal_admission_child.py")
    completed = subprocess.run(
        [sys.executable, str(helper)],
        env=child_env(binding, raw),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(completed.stdout)


def test_genuine_spawned_child_admits_and_scrubs_secret(active_run):
    conn, binding = active_run
    raw = issue(conn, binding)
    result = run_child(binding, raw)
    assert result == {
        "admitted": True,
        "current": True,
        "env_removed": True,
        "has_raw_attribute": False,
        "late_child_has_secret": False,
        "profile": "qa",
        "run_id": binding["run_id"],
        "task_id": binding["task_id"],
    }
    assert len(event_rows(conn, "principal_launch_consumed")) == 1


def test_cli_startup_helper_captures_consumes_and_registers(active_run, monkeypatch):
    from hermes_cli import main as cli_main

    conn, binding = active_run
    raw = issue(conn, binding)
    child_env_values = child_env(binding, raw)
    for key, value in child_env_values.items():
        monkeypatch.setenv(key, value)
    cli_main._capture_and_admit_principal_launch()
    principal = kp.current_principal()
    assert principal is not None
    assert principal.task_id == binding["task_id"]
    assert principal.run_id == binding["run_id"]
    assert principal.profile == binding["profile"]
    assert kp._LAUNCH_CAPABILITY_ENV not in os.environ


def test_issue_event_contains_hash_only_and_exact_binding(active_run):
    conn, binding = active_run
    raw = issue(conn, binding)
    row = event_rows(conn, "principal_launch_issued")[0]
    payload = json.loads(row["payload"])
    assert row["task_id"] == binding["task_id"]
    assert row["run_id"] == binding["run_id"]
    assert payload["version"] == 1
    assert payload["task_id"] == binding["task_id"]
    assert payload["run_id"] == binding["run_id"]
    assert payload["expected_profile"] == "qa"
    assert payload["claim_lock"] == binding["claim_lock"]
    assert payload["board"] == "default"
    assert len(payload["capability_sha256"]) == 64
    assert raw not in row["payload"]
    assert raw not in json.dumps([dict(r) for r in event_rows(conn, "principal_launch_issued")])


def test_same_active_run_second_issue_is_denied(active_run):
    conn, binding = active_run
    issue(conn, binding)

    with pytest.raises(ValueError, match="already has a principal launch issue"):
        issue(conn, binding)

    assert len(event_rows(conn, "principal_launch_issued")) == 1


@pytest.mark.parametrize("prior_state", ["consumed", "expired", "revoked"])
def test_same_run_remint_is_denied_after_terminal_issue_state(
    active_run, monkeypatch, prior_state,
):
    conn, binding = active_run
    if prior_state == "expired":
        monkeypatch.setattr(kp.time, "time", lambda: 1000)
        raw = issue(conn, binding, startup_timeout_seconds=1)
        monkeypatch.setattr(kp.time, "time", lambda: 1002)
    else:
        raw = issue(conn, binding)

    if prior_state == "consumed":
        assert kp.admit_captured_launch(capture(monkeypatch, binding, raw)) is not None
        kp._clear_current_principal_for_tests()
    elif prior_state == "revoked":
        issued = event_rows(conn, "principal_launch_issued")[0]
        with kb.write_txn(conn):
            kb._append_event(
                conn, binding["task_id"], "principal_launch_revoked",
                {"version": 1, "issue_event_id": int(issued["id"])},
                run_id=binding["run_id"],
            )

    with pytest.raises(ValueError, match="already has a principal launch issue"):
        issue(conn, binding)
    assert len(event_rows(conn, "principal_launch_issued")) == 1


def test_legacy_multiple_issues_fail_closed_on_consume(active_run, monkeypatch):
    conn, binding = active_run
    raw = issue(conn, binding)
    first_payload = json.loads(event_rows(conn, "principal_launch_issued")[0]["payload"])
    duplicate = dict(first_payload)
    duplicate["capability_sha256"] = hashlib.sha256(b"legacy-second-grant").hexdigest()
    with kb.write_txn(conn):
        kb._append_event(
            conn, binding["task_id"], "principal_launch_issued", duplicate,
            run_id=binding["run_id"],
        )

    assert kp.admit_captured_launch(capture(monkeypatch, binding, raw)) is None
    assert event_rows(conn, "principal_launch_consumed") == []


def test_two_distinct_runs_may_each_have_one_issue(active_run):
    conn, first = active_run
    first_raw = issue(conn, first)
    second_task_id = kb.create_task(conn, title="second launch run", assignee="qa")
    second_task = kb.claim_task(conn, second_task_id, claimer="test-host:200")
    assert second_task is not None and second_task.current_run_id and second_task.claim_lock
    second = {
        "board": "default",
        "db_path": first["db_path"],
        "task_id": second_task_id,
        "run_id": int(second_task.current_run_id),
        "profile": "qa",
        "claim_lock": second_task.claim_lock,
    }

    second_raw = issue(conn, second)

    assert first_raw != second_raw
    assert len(event_rows(conn, "principal_launch_issued")) == 2


def test_ids_without_secret_do_not_admit(active_run, monkeypatch):
    _conn, binding = active_run
    assert capture(monkeypatch, binding, raw=None) is None
    assert kp.current_principal() is None


def test_wrong_secret_does_not_admit(active_run, monkeypatch):
    conn, binding = active_run
    issue(conn, binding)
    captured = capture(monkeypatch, binding, "0" * 64)
    assert kp.admit_captured_launch(captured) is None
    assert kp.current_principal() is None


@pytest.mark.parametrize("field,env_name,value", [
    ("run", "HERMES_KANBAN_RUN_ID", "999999"),
    ("profile", "HERMES_PROFILE", "company"),
    ("board", "HERMES_KANBAN_BOARD", "other"),
])
def test_exact_binding_mismatch_does_not_admit(active_run, monkeypatch, field, env_name, value):
    conn, binding = active_run
    raw = issue(conn, binding)
    captured = capture(monkeypatch, binding, raw, **{env_name: value})
    assert kp.admit_captured_launch(captured) is None, field
    assert kp.current_principal() is None


def test_reclaim_before_consume_does_not_admit(active_run, monkeypatch):
    conn, binding = active_run
    raw = issue(conn, binding)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET status='released', outcome='reclaimed', ended_at=1, claim_lock=NULL WHERE id=?",
            (binding["run_id"],),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL WHERE id=?",
            (binding["task_id"],),
        )
    assert kp.admit_captured_launch(capture(monkeypatch, binding, raw)) is None


def test_completion_before_consume_does_not_admit(active_run, monkeypatch):
    conn, binding = active_run
    raw = issue(conn, binding)
    assert kb.complete_task(
        conn, binding["task_id"], summary="done", expected_run_id=binding["run_id"],
        fire_lifecycle_hook=False,
    )
    assert kp.admit_captured_launch(capture(monkeypatch, binding, raw)) is None


def test_expired_claim_does_not_admit_even_if_rows_say_running(active_run, monkeypatch):
    conn, binding = active_run
    raw = issue(conn, binding)
    expired = int(time.time()) - 1
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET claim_expires=? WHERE id=?",
            (expired, binding["task_id"]),
        )
        conn.execute(
            "UPDATE task_runs SET claim_expires=? WHERE id=?",
            (expired, binding["run_id"]),
        )
    assert kp.admit_captured_launch(capture(monkeypatch, binding, raw)) is None


def test_replay_after_consume_does_not_admit(active_run, monkeypatch):
    conn, binding = active_run
    raw = issue(conn, binding)
    assert kp.admit_captured_launch(capture(monkeypatch, binding, raw)) is not None
    kp._clear_current_principal_for_tests()
    assert kp.admit_captured_launch(capture(monkeypatch, binding, raw)) is None
    assert len(event_rows(conn, "principal_launch_consumed")) == 1


def test_expired_issue_does_not_admit(active_run, monkeypatch):
    conn, binding = active_run
    monkeypatch.setattr(kp.time, "time", lambda: 1000)
    raw = issue(conn, binding, startup_timeout_seconds=1)
    monkeypatch.setattr(kp.time, "time", lambda: 1002)
    assert kp.admit_captured_launch(capture(monkeypatch, binding, raw)) is None


def test_revoked_issue_does_not_admit(active_run, monkeypatch):
    conn, binding = active_run
    raw = issue(conn, binding)
    issued = event_rows(conn, "principal_launch_issued")[0]
    with kb.write_txn(conn):
        kb._append_event(
            conn, binding["task_id"], "principal_launch_revoked",
            {"version": 1, "issue_event_id": int(issued["id"])}, run_id=binding["run_id"],
        )
    assert kp.admit_captured_launch(capture(monkeypatch, binding, raw)) is None


def test_restart_without_fresh_capability_has_no_principal(active_run, monkeypatch):
    conn, binding = active_run
    raw = issue(conn, binding)
    assert kp.admit_captured_launch(capture(monkeypatch, binding, raw)) is not None
    kp._clear_current_principal_for_tests()
    assert capture(monkeypatch, binding, raw=None) is None
    assert kp.current_principal() is None


def test_dashboard_helper_json_and_forged_objects_are_not_principals():
    forged = {
        "board": "default", "board_identity": "sqlite-file:forged",
        "task_id": "t_x", "run_id": 1, "profile": "qa",
        "claim_lock": "forged", "issue_event_id": 1,
    }
    assert not kp._is_registered_principal(forged)
    forged_object = kp.RuntimePrincipal(**forged)
    assert not kp._is_registered_principal(forged_object)
    assert kp.current_principal() is None


def test_delegated_child_context_cannot_use_parent_principal(active_run, monkeypatch):
    from agent.delegation_context import delegated_child_context

    conn, binding = active_run
    raw = issue(conn, binding)
    principal = kp.admit_captured_launch(capture(monkeypatch, binding, raw))
    assert principal is not None and kp.current_principal() is principal
    assert kp._is_registered_principal(principal)
    clone = kp.RuntimePrincipal(
        board=principal.board,
        board_identity=principal.board_identity,
        task_id=principal.task_id,
        run_id=principal.run_id,
        profile=principal.profile,
        claim_lock=principal.claim_lock,
        issue_event_id=principal.issue_event_id,
    )
    assert not kp._is_registered_principal(clone)
    with delegated_child_context("child-session"):
        assert kp.current_principal() is None
    assert kp.current_principal() is principal


def test_non_dispatcher_context_hides_and_restores_principal(active_run, monkeypatch):
    from agent.delegation_context import (
        delegated_child_context,
        non_dispatcher_owned_context,
    )

    conn, binding = active_run
    principal = kp.admit_captured_launch(capture(monkeypatch, binding, issue(conn, binding)))
    assert principal is not None
    assert kp.current_principal() is principal

    with delegated_child_context("child-session"):
        assert kp.current_principal() is None
    with non_dispatcher_owned_context():
        assert kp.current_principal() is None
    assert kp.current_principal() is principal

    copied = contextvars.copy_context()
    assert copied.run(kp.current_principal) is principal

    async def inherited_owner_task():
        return await asyncio.create_task(_read_principal())

    async def _read_principal():
        return kp.current_principal()

    assert asyncio.run(inherited_owner_task()) is principal


def test_admission_is_denied_in_non_dispatcher_context(active_run, monkeypatch):
    from agent.delegation_context import non_dispatcher_owned_context

    conn, binding = active_run
    raw = issue(conn, binding)
    captured = capture(monkeypatch, binding, raw)
    with non_dispatcher_owned_context():
        assert kp.admit_captured_launch(captured) is None
        assert kp.current_principal() is None
    assert event_rows(conn, "principal_launch_consumed") == []


def test_startup_deadline_is_exclusive(active_run, monkeypatch):
    conn, binding = active_run
    monkeypatch.setattr(kp.time, "time", lambda: 1000)
    just_before = issue(conn, binding, startup_timeout_seconds=2)
    monkeypatch.setattr(kp.time, "time", lambda: 1001.999)
    assert kp.admit_captured_launch(capture(monkeypatch, binding, just_before)) is not None

    second_task_id = kb.create_task(conn, title="deadline exact", assignee="qa")
    second_task = kb.claim_task(conn, second_task_id, claimer="deadline:exact")
    exact = {
        **binding,
        "task_id": second_task_id,
        "run_id": int(second_task.current_run_id),
        "claim_lock": second_task.claim_lock,
    }
    monkeypatch.setattr(kp.time, "time", lambda: 1002)
    exact_raw = issue(conn, exact, startup_timeout_seconds=1)
    monkeypatch.setattr(kp.time, "time", lambda: 1003)
    assert kp.admit_captured_launch(capture(monkeypatch, exact, exact_raw)) is None

    third_task_id = kb.create_task(conn, title="deadline after", assignee="qa")
    third_task = kb.claim_task(conn, third_task_id, claimer="deadline:after")
    after = {
        **binding,
        "task_id": third_task_id,
        "run_id": int(third_task.current_run_id),
        "claim_lock": third_task.claim_lock,
    }
    monkeypatch.setattr(kp.time, "time", lambda: 1004)
    after_raw = issue(conn, after, startup_timeout_seconds=1)
    monkeypatch.setattr(kp.time, "time", lambda: 1006)
    assert kp.admit_captured_launch(capture(monkeypatch, after, after_raw)) is None


def test_concurrent_real_process_consume_has_exactly_one_winner(active_run):
    conn, binding = active_run
    raw = issue(conn, binding)
    helper = Path(__file__).with_name("_principal_admission_child.py")
    env = child_env(binding, raw)
    contenders = [
        subprocess.Popen(
            [sys.executable, str(helper)], env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        for _ in range(2)
    ]
    results = []
    for proc in contenders:
        stdout, stderr = proc.communicate(timeout=30)
        assert proc.returncode == 0, stderr
        results.append(json.loads(stdout))
    assert sorted(item["admitted"] for item in results) == [False, True]
    assert len(event_rows(conn, "principal_launch_consumed")) == 1


def test_relative_and_case_path_spellings_use_canonical_admission_path(
    active_run, monkeypatch,
):
    conn, binding = active_run
    raw = issue(conn, binding)
    monkeypatch.chdir(binding["db_path"].parent)
    relative = dict(binding, db_path=Path(binding["db_path"].name))
    assert kp.admit_captured_launch(capture(monkeypatch, relative, raw)) is not None

    if os.name == "nt":
        kp._clear_current_principal_for_tests()
        second_task_id = kb.create_task(conn, title="case path launch", assignee="qa")
        second_task = kb.claim_task(conn, second_task_id, claimer="test-host:case")
        second = {
            **binding,
            "task_id": second_task_id,
            "run_id": int(second_task.current_run_id),
            "claim_lock": second_task.claim_lock,
        }
        second_raw = issue(conn, second)
        case_variant = dict(second, db_path=Path(str(binding["db_path"]).upper()))
        assert kp.admit_captured_launch(capture(monkeypatch, case_variant, second_raw)) is not None


def test_wrong_and_copied_database_paths_do_not_admit(active_run, tmp_path, monkeypatch):
    conn, binding = active_run
    raw = issue(conn, binding)

    independent_path = tmp_path / "independent.db"
    independent = kbc.connect(db_path=independent_path)
    independent.close()
    assert kp.admit_captured_launch(capture(
        monkeypatch, dict(binding, db_path=independent_path), raw,
    )) is None

    copied_path = tmp_path / "copied.db"
    with sqlite3.connect(copied_path) as copied:
        conn.backup(copied)
    assert kp.admit_captured_launch(capture(
        monkeypatch, dict(binding, db_path=copied_path), raw,
    )) is None
    assert kp.admit_captured_launch(capture(monkeypatch, binding, raw)) is not None


def test_issuance_rejects_connection_opened_through_symlink(active_run, tmp_path):
    conn, binding = active_run
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    alias = tmp_path / "issuance-alias.db"
    try:
        alias.symlink_to(binding["db_path"])
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    alias_conn = kbc.connect(db_path=alias)
    try:
        with pytest.raises(ValueError, match="canonical SQLite path"):
            issue(alias_conn, binding)
        assert event_rows(conn, "principal_launch_issued") == []
    finally:
        alias_conn.close()


def test_symlink_and_real_path_share_one_global_consume_domain(
    active_run, tmp_path,
):
    conn, binding = active_run
    raw = issue(conn, binding)
    assert len(event_rows(conn, "principal_launch_issued")) == 1
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    alias = tmp_path / "consume-alias.db"
    try:
        alias.symlink_to(binding["db_path"])
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    # Keep the noncanonical connection alive to reproduce Windows' distinct
    # alias-named WAL namespace while both consumers must open the real path.
    alias_conn = kbc.connect(db_path=alias)
    helper = Path(__file__).with_name("_principal_admission_child.py")
    contenders = [
        subprocess.Popen(
            [sys.executable, str(helper)],
            env=child_env(dict(binding, db_path=path), raw),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for path in (binding["db_path"], alias)
    ]
    try:
        results = []
        for proc in contenders:
            stdout, stderr = proc.communicate(timeout=30)
            assert proc.returncode == 0, stderr
            results.append(json.loads(stdout)["admitted"])
        assert sorted(results) == [False, True]
        assert len(event_rows(conn, "principal_launch_consumed")) == 1
    finally:
        alias_conn.close()


def test_consume_event_failure_creates_no_principal(active_run, monkeypatch):
    conn, binding = active_run
    raw = issue(conn, binding)
    original = kb._append_event

    def fail_consumed(conn_arg, task_id, kind, payload=None, *, run_id=None):
        if kind == "principal_launch_consumed":
            raise sqlite3.OperationalError("synthetic insert failure")
        return original(conn_arg, task_id, kind, payload, run_id=run_id)

    monkeypatch.setattr(kb, "_append_event", fail_consumed)
    assert kp.admit_captured_launch(capture(monkeypatch, binding, raw)) is None
    assert kp.current_principal() is None
    assert event_rows(conn, "principal_launch_consumed") == []


def test_consume_commit_failure_creates_no_principal(active_run, monkeypatch):
    conn, binding = active_run
    raw = issue(conn, binding)

    @contextlib.contextmanager
    def fail_commit(conn_arg, *, allow_nested=False):
        assert allow_nested is False
        conn_arg.execute("BEGIN IMMEDIATE")
        try:
            yield
            conn_arg.execute("ROLLBACK")
            raise sqlite3.OperationalError("synthetic commit failure")
        except Exception:
            if conn_arg.in_transaction:
                conn_arg.execute("ROLLBACK")
            raise

    monkeypatch.setattr(kb, "write_txn", fail_commit)
    assert kp.admit_captured_launch(capture(monkeypatch, binding, raw)) is None
    assert kp.current_principal() is None
    assert event_rows(conn, "principal_launch_consumed") == []


def test_capture_removes_even_invalid_secret(monkeypatch):
    monkeypatch.setenv(kp._LAUNCH_CAPABILITY_ENV, "not-a-token")
    assert kp.capture_startup_capability() is None
    assert kp._LAUNCH_CAPABILITY_ENV not in os.environ


def test_capture_missing_binding_removes_valid_secret():
    env = {kp._LAUNCH_CAPABILITY_ENV: "ab" * 32}
    assert kp.capture_startup_capability(env) is None
    assert kp._LAUNCH_CAPABILITY_ENV not in env


def test_captured_repr_never_contains_raw(active_run, monkeypatch):
    conn, binding = active_run
    raw = issue(conn, binding)
    captured = capture(monkeypatch, binding, raw)
    assert captured is not None
    assert raw not in repr(captured)
    assert kp.admit_captured_launch(captured) is not None
    assert raw not in repr(captured)


def test_main_attempts_admission_before_recovery_and_fast_launch(monkeypatch):
    from hermes_cli import main as cli_main

    calls = []
    monkeypatch.setattr(cli_main, "_capture_and_admit_principal_launch", lambda: calls.append("admit"))
    monkeypatch.setattr(cli_main, "_set_process_title", lambda: calls.append("title"))
    monkeypatch.setattr(cli_main, "_advertise_agent_env", lambda: calls.append("advertise"))
    monkeypatch.setattr(cli_main, "_cleanup_quarantined_exes", lambda: calls.append("cleanup"))
    monkeypatch.setattr(cli_main, "_sweep_stale_bytecode_if_checkout_changed", lambda: calls.append("sweep"))
    monkeypatch.setattr(cli_main, "_recover_from_interrupted_install", lambda: calls.append("recovery"))
    monkeypatch.setattr(cli_main, "_try_termux_fast_tui_launch", lambda: False)
    monkeypatch.setattr(cli_main, "_try_termux_fast_cli_launch", lambda: False)
    monkeypatch.setattr(cli_main, "_try_fast_serve_launch", lambda: False)
    monkeypatch.setattr(cli_main, "_try_fast_chat_launch", lambda: calls.append("fast") or True)
    monkeypatch.setattr(sys, "argv", ["hermes"])
    cli_main.main()
    assert calls[0] == "admit"
    assert calls.index("admit") < calls.index("recovery") < calls.index("fast")


def test_custom_spawn_path_gets_no_launch_issue(active_run, tmp_path):
    conn, binding = active_run
    calls = []
    # The task is already running, so exercise the explicit compatibility fact directly:
    # custom spawn dispatch goes through _call_spawn_fn and receives only the legacy args.
    task = kb.get_task(conn, binding["task_id"])
    assert task is not None
    assert kbd._call_spawn_fn(
        lambda *args, **kwargs: calls.append((args, kwargs)) or 123,
        task, str(tmp_path), "default",
    ) == 123
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert len(args) == 2
    assert kwargs == {}
    assert event_rows(conn, "principal_launch_issued") == []


def test_dispatcher_custom_spawn_never_issues_capability(tmp_path, monkeypatch):
    db_path = tmp_path / "custom-kanban.db"
    conn = kbc.connect(db_path=db_path)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: (lambda _profile: True))
    monkeypatch.setattr(kbd._kbw, "resolve_workspace", lambda _task, board=None: tmp_path)
    task_id = kb.create_task(conn, title="legacy custom", assignee="qa")
    calls = []
    try:
        result = kbd.dispatch_once(
            conn, board="default", max_spawn=1,
            spawn_fn=lambda task, workspace: calls.append((task.id, workspace)) or 7654,
        )
        assert [item[0] for item in result.spawned] == [task_id]
        assert calls == [(task_id, str(tmp_path))]
        assert event_rows(conn, "principal_launch_issued") == []
    finally:
        conn.close()


def test_builtin_spawn_puts_secret_only_in_private_child_env(active_run, tmp_path, monkeypatch):
    conn, binding = active_run
    task = kb.get_task(conn, binding["task_id"])
    assert task is not None
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    raw = "ab" * 32
    seen = {}

    class Proc:
        pid = 4242

    def fake_popen(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["env"] = dict(kwargs["env"])
        return Proc()

    monkeypatch.delenv(kp._LAUNCH_CAPABILITY_ENV, raising=False)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(kbd, "_worker_argv", lambda *args: ["hermes", "chat"])
    monkeypatch.setattr(kbd, "_restart_safe_worker_argv", lambda _task, cmd: cmd)
    monkeypatch.setattr(kbd, "_open_worker_log", lambda *args: contextlib.nullcontext())
    monkeypatch.setattr("hermes_cli.profiles.resolve_profile_env", lambda _profile: str(tmp_path))
    monkeypatch.setattr("tools.process_registry.systemd_user_bus_env", lambda env: env)
    assert kbd._default_spawn(
        task, str(workspace), board="default", _launch_issuer=lambda: raw,
    ) == 4242
    assert seen["env"][kp._LAUNCH_CAPABILITY_ENV] == raw
    assert all(raw not in str(arg) for arg in seen["cmd"])
    assert kp._LAUNCH_CAPABILITY_ENV not in os.environ


def test_dispatcher_default_path_issues_then_real_child_consumes(tmp_path, monkeypatch):
    db_path = tmp_path / "dispatch-kanban.db"
    conn = kbc.connect(db_path=db_path)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: (lambda _profile: True))
    monkeypatch.setattr(kbd._kbw, "resolve_workspace", lambda _task, board=None: tmp_path)
    task_id = kb.create_task(conn, title="dispatch admission", assignee="qa")
    child_result = {}

    def supported_spawn(task, workspace, *, board=None, _launch_issuer=None):
        assert _launch_issuer is not None
        raw = _launch_issuer()
        binding = {
            "board": board or "default",
            "db_path": db_path,
            "task_id": task.id,
            "run_id": int(task.current_run_id),
            "profile": task.assignee,
            "claim_lock": task.claim_lock,
        }
        child_result.update(run_child(binding, raw))
        return 5678

    monkeypatch.setattr(kbd, "_default_spawn", supported_spawn)
    try:
        result = kbd.dispatch_once(conn, board="default", max_spawn=1)
        assert [item[0] for item in result.spawned] == [task_id]
        assert child_result["admitted"] is True
        assert child_result["env_removed"] is True
        assert len(event_rows(conn, "principal_launch_issued")) == 1
        assert len(event_rows(conn, "principal_launch_consumed")) == 1
    finally:
        conn.close()


def test_wrapped_spawn_never_calls_launch_issuer(active_run, tmp_path, monkeypatch):
    conn, binding = active_run
    task = kb.get_task(conn, binding["task_id"])
    workspace = tmp_path / "wrapped-workspace"
    workspace.mkdir()
    calls = []
    seen = {}

    class Proc:
        pid = 42

    def fake_popen(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["env"] = dict(kwargs["env"])
        return Proc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(kbd, "_worker_argv", lambda *args: ["hermes", "chat"])
    monkeypatch.setattr(kbd, "_restart_safe_worker_argv", lambda _task, cmd: ["wrapper", *cmd])
    monkeypatch.setattr(kbd, "_open_worker_log", lambda *args: contextlib.nullcontext())
    monkeypatch.setattr("hermes_cli.profiles.resolve_profile_env", lambda _profile: str(tmp_path))
    monkeypatch.setattr("tools.process_registry.systemd_user_bus_env", lambda env: env)
    assert kbd._default_spawn(
        task, str(workspace), board="default",
        _launch_issuer=lambda: calls.append("issued") or ("ab" * 32),
    ) == 42
    assert calls == []
    assert kp._LAUNCH_CAPABILITY_ENV not in seen["env"]


def test_issue_failure_falls_back_to_legacy_spawn(active_run, tmp_path, monkeypatch):
    conn, binding = active_run
    task = kb.get_task(conn, binding["task_id"])
    workspace = tmp_path / "legacy-workspace"
    workspace.mkdir()
    seen = {}

    class Proc:
        pid = 43

    def fake_popen(_cmd, **kwargs):
        seen["env"] = dict(kwargs["env"])
        return Proc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(kbd, "_worker_argv", lambda *args: ["hermes", "chat"])
    monkeypatch.setattr(kbd, "_restart_safe_worker_argv", lambda _task, cmd: cmd)
    monkeypatch.setattr(kbd, "_open_worker_log", lambda *args: contextlib.nullcontext())
    monkeypatch.setattr("hermes_cli.profiles.resolve_profile_env", lambda _profile: str(tmp_path))
    monkeypatch.setattr("tools.process_registry.systemd_user_bus_env", lambda env: env)

    def unavailable():
        raise RuntimeError("synthetic admission unavailable")

    assert kbd._default_spawn(
        task, str(workspace), board="default", _launch_issuer=unavailable,
    ) == 43
    assert kp._LAUNCH_CAPABILITY_ENV not in seen["env"]


def test_popen_failure_leaves_one_pending_issue_without_same_run_remint(
    active_run, tmp_path, monkeypatch,
):
    conn, binding = active_run
    task = kb.get_task(conn, binding["task_id"])
    workspace = tmp_path / "failed-spawn-workspace"
    workspace.mkdir()
    held = {}
    real_popen = subprocess.Popen

    def launch_issuer():
        raw = issue(conn, binding)
        held["raw"] = raw
        return raw

    def fail_popen(_cmd, **kwargs):
        held["env"] = kwargs["env"]
        raise OSError("synthetic Popen failure")

    monkeypatch.setattr(subprocess, "Popen", fail_popen)
    monkeypatch.setattr(kbd, "_worker_argv", lambda *args: ["hermes", "chat"])
    monkeypatch.setattr(kbd, "_restart_safe_worker_argv", lambda _task, cmd: cmd)
    monkeypatch.setattr(kbd, "_open_worker_log", lambda *args: contextlib.nullcontext())
    monkeypatch.setattr("hermes_cli.profiles.resolve_profile_env", lambda _profile: str(tmp_path))
    monkeypatch.setattr("tools.process_registry.systemd_user_bus_env", lambda env: env)

    with pytest.raises(OSError, match="synthetic Popen failure"):
        kbd._default_spawn(
            task, str(workspace), board="default", _launch_issuer=launch_issuer,
        )

    assert kp._LAUNCH_CAPABILITY_ENV not in held["env"]
    assert len(event_rows(conn, "principal_launch_issued")) == 1
    with pytest.raises(ValueError, match="already has a principal launch issue"):
        issue(conn, binding)

    # A retained raw value still represents only the one pending issue. Even if
    # two processes obtained it, the existing BEGIN IMMEDIATE consume wins once.
    monkeypatch.setattr(subprocess, "Popen", real_popen)
    helper = Path(__file__).with_name("_principal_admission_child.py")
    contenders = [
        subprocess.Popen(
            [sys.executable, str(helper)], env=child_env(binding, held["raw"]),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for _ in range(2)
    ]
    results = []
    for proc in contenders:
        stdout, stderr = proc.communicate(timeout=30)
        assert proc.returncode == 0, stderr
        results.append(json.loads(stdout)["admitted"])
    assert sorted(results) == [False, True]
    assert len(event_rows(conn, "principal_launch_consumed")) == 1


def test_runtime_principal_is_not_json_serializable(active_run, monkeypatch):
    conn, binding = active_run
    raw = issue(conn, binding)
    principal = kp.admit_captured_launch(capture(monkeypatch, binding, raw))
    assert principal is not None
    with pytest.raises(TypeError):
        json.dumps(principal)


def test_deterministic_raw_secret_never_reaches_db_repr_argv_log_or_exception(
    active_run, tmp_path, monkeypatch, caplog,
):
    conn, binding = active_run
    deterministic_raw = "d15ea5ed" * 8
    monkeypatch.setattr(kp.secrets, "token_hex", lambda size: deterministic_raw)
    raw = issue(conn, binding)
    assert raw == deterministic_raw

    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{binding['db_path']}{suffix}")
        if candidate.exists():
            assert deterministic_raw.encode("ascii") not in candidate.read_bytes()

    captured = capture(monkeypatch, binding, raw, HERMES_KANBAN_RUN_ID="999999")
    assert deterministic_raw not in repr(captured)
    assert kp.admit_captured_launch(captured) is None
    assert deterministic_raw not in caplog.text

    task = kb.get_task(conn, binding["task_id"])
    workspace = tmp_path / "leak-workspace"
    workspace.mkdir()
    seen = {}

    class Proc:
        pid = 44

    def fake_popen(cmd, **kwargs):
        seen["cmd"] = list(cmd)
        seen["env"] = dict(kwargs["env"])
        return Proc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(kbd, "_worker_argv", lambda *args: ["hermes", "chat"])
    monkeypatch.setattr(kbd, "_restart_safe_worker_argv", lambda _task, cmd: cmd)
    monkeypatch.setattr(kbd, "_open_worker_log", lambda *args: contextlib.nullcontext())
    monkeypatch.setattr("hermes_cli.profiles.resolve_profile_env", lambda _profile: str(tmp_path))
    monkeypatch.setattr("tools.process_registry.systemd_user_bus_env", lambda env: env)

    def secret_exception():
        raise RuntimeError(deterministic_raw)

    assert kbd._default_spawn(
        task, str(workspace), board="default", _launch_issuer=secret_exception,
    ) == 44
    assert all(deterministic_raw not in str(arg) for arg in seen["cmd"])
    assert kp._LAUNCH_CAPABILITY_ENV not in seen["env"]
    assert deterministic_raw not in caplog.text
