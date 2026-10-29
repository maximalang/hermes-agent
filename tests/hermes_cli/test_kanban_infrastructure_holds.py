"""Infrastructure failures hold work, not task retry budgets or quota loops."""
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli.kanban_failure_policy import classify_infrastructure


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    dispatch._recent_worker_exits.clear()
    kb.init_db()


@pytest.mark.parametrize("error,kind", [
    ("OpenBLAS error: Memory allocation still failed after 10 retries", "transient"),
    ("RuntimeError: TIMEWEB_MCP_TOKEN is not set", "capability"),
    ("ImportError: DLL load failed while importing numpy", "capability"),
    ("RateLimitError: Error code: 429", "transient"),
])
def test_spawn_failure_holds_without_burning_task_retries(home, error, kind):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="work", assignee="tech")
        kb.claim_task(conn, tid, claimer="dead:123")
        conn.execute("UPDATE tasks SET consecutive_failures=1 WHERE id=?", (tid,))
        conn.commit()
        assert dispatch._record_task_failure(
            conn, tid, error, outcome="spawn_failed", failure_limit=20,
            release_claim=True, end_run=True,
        )
        task = kb.get_task(conn, tid)
        assert (task.status, task.block_kind, task.consecutive_failures) == ("blocked", kind, 1)
        assert not task.claim_lock and not task.worker_pid
        kb.recompute_ready(conn, failure_limit=100)
        assert kb.get_task(conn, tid).status == "blocked"
        run = conn.execute("SELECT * FROM task_runs WHERE task_id=?", (tid,)).fetchone()
        assert run["outcome"] == "infrastructure_blocked"
        data = kb._json_dict(run["metadata"])
        assert data["failure_class"] == "infrastructure"
        cause = kb.get_task(conn, data["remediation_task_id"])
        assert cause.assignee == "operations" and cause.status == "triage"


@pytest.mark.parametrize("text", [
    "AssertionError: expected 429 records", "write a quota report", "MemoryError",
    "ImportError: cannot import internal_function", "The author implemented the fix",
])
def test_unknown_task_errors_are_not_infrastructure(text):
    assert classify_infrastructure(text) is None


def _dead(conn, rc, monkeypatch):
    tid = kb.create_task(conn, title="dead worker", assignee="tech")
    host = kb._claimer_id().split(":", 1)[0]
    kb.claim_task(conn, tid, claimer=f"{host}:w999991")
    conn.execute("UPDATE tasks SET worker_pid=999991, worker_started_at=NULL, started_at=? WHERE id=?",
                 (int(time.time()) - 120, tid))
    conn.commit()
    monkeypatch.setattr(dispatch, "_worker_log_exit_code", lambda *args, **kw: rc)
    return tid


def test_quota_exit_is_a_hold_not_a_respawn_loop(home, monkeypatch):
    with kbc.connect() as conn:
        tid = _dead(conn, kb.KANBAN_RATE_LIMIT_EXIT_CODE, monkeypatch)
        dispatch.detect_crashed_workers(conn)
        kb.recompute_ready(conn, failure_limit=100)
        assert kb.get_task(conn, tid).status == "blocked"
        assert kb.get_task(conn, tid).block_kind == "transient"
        assert kb.get_task(conn, tid).consecutive_failures == 0
        for _ in range(6):
            dispatch.detect_crashed_workers(conn)
            kb.recompute_ready(conn, failure_limit=100)
        assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0] == 1


def test_crash_signature_uses_current_run_only(home, monkeypatch):
    from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER
    with kbc.connect() as conn:
        tid = _dead(conn, 1, monkeypatch)
        log = kb.worker_log_path(tid)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("RuntimeError: TIMEWEB_MCP_TOKEN is not set\n" +
                       KANBAN_WORKER_EXIT_TRAILER + "1\nAssertionError: task failed\n" +
                       KANBAN_WORKER_EXIT_TRAILER + "1\n", encoding="utf-8")
        dispatch.detect_crashed_workers(conn)
        assert kb.get_task(conn, tid).consecutive_failures == 1
        assert kb.get_task(conn, tid).status == "ready"


def test_remediation_dedupes_across_tasks_and_host_refusal(home):
    with kbc.connect() as conn:
        for _ in range(2):
            tid = kb.create_task(conn, title="host", assignee="tech")
            kb.claim_task(conn, tid, claimer="dead:123")
            dispatch._record_task_failure(conn, tid, "scope not available", outcome="spawn_failed",
                                          failure_limit=1, release_claim=True, end_run=True,
                                          infrastructure=True)
            assert kb.get_task(conn, tid).status == "blocked"
            assert kb.get_task(conn, tid).consecutive_failures == 0
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE idempotency_key LIKE 'infra-cause:%'").fetchone()[0] == 1


def test_crash_log_blocks_on_current_infra_signature(home, monkeypatch):
    with kbc.connect() as conn:
        tid = _dead(conn, 1, monkeypatch)
        log = kb.worker_log_path(tid)
        log.parent.mkdir(parents=True, exist_ok=True)
        from hermes_cli.kanban_failure_policy import RUN_START
        run_id = kb.get_task(conn, tid).current_run_id
        log.write_bytes(f"legacy quota output\n{RUN_START}{run_id}\nOpenBLAS error: Memory allocation still failed after 10 retries\n".encode())
        dispatch.detect_crashed_workers(conn)
        assert kb.get_task(conn, tid).block_kind == "transient"
        assert kb.get_task(conn, tid).consecutive_failures == 0


def test_hold_and_remediation_rollback_together(home, monkeypatch):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="atomic", assignee="tech")
        kb.claim_task(conn, tid, claimer="dead:123")
        original = kb._append_event
        def fail_event(conn, task_id, kind, *args, **kwargs):
            if kind == "infrastructure_blocked":
                raise RuntimeError("commit interrupted")
            return original(conn, task_id, kind, *args, **kwargs)
        monkeypatch.setattr(kb, "_append_event", fail_event)
        with pytest.raises(RuntimeError, match="commit interrupted"):
            dispatch._record_task_failure(conn, tid, "scope unavailable", outcome="spawn_failed",
                                          infrastructure=True, end_run=True, release_claim=True)
        assert kb.get_task(conn, tid).status == "running"
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE idempotency_key LIKE 'infra-cause:%'").fetchone()[0] == 0
        assert conn.execute("SELECT ended_at FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0] is None


def test_concurrent_idempotency_serializes_lookup_with_insert(home, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    import threading
    barrier = threading.Barrier(2)
    local = threading.local()
    original = kb.write_txn
    @contextmanager
    def synchronized(conn, **kwargs):
        if not getattr(local, "entered", False):
            local.entered = True
            barrier.wait(timeout=10)
        with original(conn, **kwargs):
            yield
    monkeypatch.setattr(kb, "write_txn", synchronized)
    def create(_):
        with kbc.connect() as conn:
            return kb.create_task(conn, title="same signal", assignee="operations",
                                  triage=True, idempotency_key="concurrent-signal")
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(create, range(2)))
    assert ids[0] == ids[1]
    with kbc.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE idempotency_key='concurrent-signal'").fetchone()[0] == 1


def test_new_run_without_output_does_not_inherit_prior_quota_footer(home):
    from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="new run", assignee="tech")
        task = kb.claim_task(conn, tid, claimer="dead:1")
        path = kb.worker_log_path(tid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(KANBAN_WORKER_EXIT_TRAILER + "75\n", encoding="utf-8")
        with dispatch._open_worker_log(task, None):
            pass
        assert dispatch._worker_log_exit_code(tid) is None


def test_review_phase_survives_infrastructure_hold_and_explicit_unblock(home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="review", assignee="tech")
        task = kb.claim_task(conn, tid, claimer="dead:1")
        kb.request_review(conn, tid, summary="review me", reviewer="qa", expected_run_id=task.current_run_id)
        kb.claim_review_task(conn, tid, claimer="dead:2")
        dispatch._record_task_failure(conn, tid, "RateLimitError: 429", outcome="spawn_failed",
                                      end_run=True, release_claim=True)
        assert kb.get_task(conn, tid).status == "blocked"
        kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).status == "review"


def test_task_logic_still_consumes_existing_failure_limit(home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="logic", assignee="tech")
        dispatch._record_task_failure(conn, tid, "AssertionError: wrong answer", outcome="crashed",
                                      failure_limit=1, release_claim=False, end_run=False)
        task = kb.get_task(conn, tid)
        assert (task.status, task.consecutive_failures) == ("blocked", 1)
        assert task.block_kind is None
