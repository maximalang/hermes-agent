"""Infra kill (gateway drain / nightly-update restart) = neutral requeue.

A long-lived dispatcher (embedded gateway watcher, ``hermes kanban daemon``)
records its in-flight workers in ``<kanban_home>/kanban/infra-drain.json``
(``write_infra_drain_marker``) right before a clean shutdown. After the
restart, the crash sweep books a dead worker that this marker lists — task_id
+ pid + verified spawn fingerprint all agreeing, AND no in-band task failure
(no reap-registry exit code, no worker-log exit trailer; or a termination
signal 1/2/9/15) — as ``infra_killed``: the card returns to its source phase
WITHOUT counting ``consecutive_failures``, so a nightly restart can never walk
a card into the circuit breaker (``gave_up`` -> blocked, operator-only).

Regression-guarded here: genuine crashes keep the old accounting. Fault
signals (SEGV), exit trailers / registry exit codes, marker mismatch (pid,
fingerprint, host), stale markers and TTL=0 all stay ``crashed`` + counted.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    kbd._recent_worker_exits.clear()
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as c:
        yield c


# Verified "<epoch>|<start>" fingerprint shape (see _process_fingerprint).
FP = "|178900000001"


def _running_worker(conn, tid: str, pid: int, fingerprint=FP) -> None:
    """Claim ``tid`` for a worker that is already dead (fixture: _pid_alive False)."""
    host = kb._claimer_id().split(":", 1)[0]
    kb.claim_task(conn, tid, claimer=f"{host}:w{pid}")
    conn.execute(
        "UPDATE tasks SET worker_pid=?, worker_started_at=?, started_at=? WHERE id=?",
        (pid, fingerprint, int(time.time()) - 120, tid),
    )
    conn.commit()


def _write_marker(entries, *, reason="test_drain", written_at=None, host=None) -> None:
    """Hand-write the drain marker (bypassing the DB-snapshot writer)."""
    marker = {
        "host": host if host is not None else kb._claimer_id().split(":", 1)[0],
        "written_at": int(time.time()) if written_at is None else written_at,
        "reason": reason,
        "dispatcher_pid": 4242,
        "workers": entries,
    }
    path = kbd._infra_drain_marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(marker), encoding="utf-8")


def _entry(tid: str, pid: int, fingerprint=FP, board="default", run_id=None) -> dict:
    return {"task_id": tid, "pid": pid, "worker_started_at": fingerprint,
            "board": board, "run_id": run_id}


def _latest_event(conn, tid: str):
    return conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (tid,),
    ).fetchone()


def _latest_run(conn, tid: str):
    return conn.execute(
        "SELECT outcome, error, metadata FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (tid,),
    ).fetchone()


def _kinds(conn, tid: str) -> list:
    return [r["kind"] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (tid,)).fetchall()]


class TestInfraDrainRequeue:
    def test_marker_writer_snapshot_requeues_without_counting(self, conn):
        """End-to-end: the shutdown writer snapshots the in-flight worker; the
        next sweep books it ``infra_killed`` — ready, no failure, no breaker."""
        tid = kb.create_task(conn, title="t", assignee="a")
        _running_worker(conn, tid, 70001)

        path = kbd.write_infra_drain_marker(reason="gateway_shutdown")
        assert path == str(kbd._infra_drain_marker_path())
        marker = json.loads(Path(path).read_text(encoding="utf-8"))
        entry = next(w for w in marker["workers"] if w["task_id"] == tid)
        assert entry["pid"] == 70001
        assert entry["worker_started_at"] == FP

        crashed = kbd.detect_crashed_workers(conn)

        assert crashed == []
        assert getattr(kbd.detect_crashed_workers, "_last_infra_killed") == [tid]
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 0
        ev = _latest_event(conn, tid)
        assert ev["kind"] == "infra_killed"
        payload = kb._json_dict(ev["payload"])
        assert payload["drain"]["reason"] == "gateway_shutdown"
        assert payload["retry_status"] == "ready"
        run = _latest_run(conn, tid)
        assert run["outcome"] == "infra_killed"
        assert "not alive" not in (run["error"] or "")
        assert "crashed" not in _kinds(conn, tid)
        assert "gave_up" not in _kinds(conn, tid)

    def test_repeated_infra_kills_never_trip_breaker(self, conn):
        """More infra kills than DEFAULT_FAILURE_LIMIT: the card stays ready and
        the counter stays at zero — a restart loop is a host problem, never the
        card's breaker (no gave_up, no blocked/triage park)."""
        tid = kb.create_task(conn, title="t", assignee="a")
        for i in range(kbd.DEFAULT_FAILURE_LIMIT + 1):
            _running_worker(conn, tid, 71000 + i, fingerprint=f"|1789000010{i}")
            kbd.write_infra_drain_marker(reason="nightly_update")
            assert kbd.detect_crashed_workers(conn) == []
            task = kb.get_task(conn, tid)
            assert task.status == "ready", f"iteration {i}"
            assert task.consecutive_failures == 0, f"iteration {i}"
        assert "gave_up" not in _kinds(conn, tid)
        assert "blocked" not in _kinds(conn, tid)

    def test_genuine_crash_without_marker_still_counts(self, conn):
        """Regression guard: no drain marker -> unchanged crashed accounting —
        failure counted, breaker trips at the limit exactly as before."""
        tid = kb.create_task(conn, title="t", assignee="a")
        _running_worker(conn, tid, 72001)

        assert kbd.detect_crashed_workers(conn) == [tid]
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 1
        assert _latest_event(conn, tid)["kind"] == "crashed"
        assert "not alive" in (_latest_run(conn, tid)["error"] or "")

        _running_worker(conn, tid, 72002, fingerprint="|17890000099")
        assert kbd.detect_crashed_workers(conn) == [tid]
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.consecutive_failures == 2
        assert "gave_up" in _kinds(conn, tid)

    def test_marker_requires_pid_match(self, conn):
        """Fail-closed identity: a marker entry for a DIFFERENT process (pid
        recycled, another spawn of the same card) never excuses this death."""
        tid = kb.create_task(conn, title="t", assignee="a")
        _running_worker(conn, tid, 73001)
        _write_marker([_entry(tid, 73999)])
        assert kbd.detect_crashed_workers(conn) == [tid]
        assert kb.get_task(conn, tid).consecutive_failures == 1

    def test_marker_fingerprint_mismatch_does_not_excuse(self, conn):
        tid = kb.create_task(conn, title="t", assignee="a")
        _running_worker(conn, tid, 74001, fingerprint="|17890000077")
        _write_marker([_entry(tid, 74001, fingerprint="|17890000055")])
        assert kbd.detect_crashed_workers(conn) == [tid]
        assert kb.get_task(conn, tid).consecutive_failures == 1

    def test_unverified_or_missing_fingerprint_fails_closed(self, conn):
        """UNVERIFIED / legacy-NULL fingerprints are identity we cannot prove —
        the marker never pardons them (refusal, not permission)."""
        tids = [kb.create_task(conn, title=f"t{i}", assignee="a") for i in range(2)]
        _running_worker(conn, tids[0], 75001, fingerprint=kbd.UNVERIFIED_WORKER_FINGERPRINT)
        _running_worker(conn, tids[1], 75002, fingerprint=None)
        kbd.write_infra_drain_marker()
        crashed = kbd.detect_crashed_workers(conn)
        assert sorted(crashed) == sorted(tids)

    def test_stale_marker_does_not_excuse(self, conn):
        tid = kb.create_task(conn, title="t", assignee="a")
        _running_worker(conn, tid, 76001)
        _write_marker(
            [_entry(tid, 76001)],
            written_at=int(time.time()) - kbd.DEFAULT_INFRA_DRAIN_MARKER_TTL_SECONDS - 60,
        )
        assert kbd.detect_crashed_workers(conn) == [tid]
        assert kb.get_task(conn, tid).consecutive_failures == 1

    def test_ttl_zero_disables_infra_classification(self, conn, monkeypatch):
        monkeypatch.setenv("HERMES_KANBAN_INFRA_DRAIN_MARKER_TTL_SECONDS", "0")
        tid = kb.create_task(conn, title="t", assignee="a")
        _running_worker(conn, tid, 77001)
        kbd.write_infra_drain_marker()
        assert kbd.detect_crashed_workers(conn) == [tid]
        assert kb.get_task(conn, tid).consecutive_failures == 1

    def test_other_host_marker_ignored(self, conn):
        tid = kb.create_task(conn, title="t", assignee="a")
        _running_worker(conn, tid, 78001)
        _write_marker([_entry(tid, 78001)], host="some-other-host")
        assert kbd.detect_crashed_workers(conn) == [tid]
        assert kb.get_task(conn, tid).consecutive_failures == 1

    def test_exit_trailer_wins_over_marker(self, conn):
        """In-band failure evidence beats the drain marker: the worker reached
        its own exit epilogue with rc=1 — a genuine crash even mid-drain."""
        tid = kb.create_task(conn, title="t", assignee="a")
        _running_worker(conn, tid, 79001)
        kbd.write_infra_drain_marker()
        log = kb.worker_log_path(tid)
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"boom\n\n{KANBAN_WORKER_EXIT_TRAILER}1\n")
        assert kbd.detect_crashed_workers(conn) == [tid]
        run = _latest_run(conn, tid)
        assert run["outcome"] == "crashed"
        assert kb._json_dict(run["metadata"]).get("exit_code") == 1
        assert kb.get_task(conn, tid).consecutive_failures == 1

    def test_signaled_term_with_marker_is_infra(self, conn):
        """POSIX drain: the reap registry saw SIGTERM (a termination signal) and
        the marker lists the process — infra, not a task failure."""
        tid = kb.create_task(conn, title="t", assignee="a")
        _running_worker(conn, tid, 80001)
        kbd.write_infra_drain_marker()
        # Raw wait-status layout: low 7 bits = signal (see _classify_worker_exit).
        kbd._recent_worker_exits[80001] = (15, time.time())
        assert kbd.detect_crashed_workers(conn) == []
        assert _latest_event(conn, tid)["kind"] == "infra_killed"
        assert kb.get_task(conn, tid).consecutive_failures == 0

    def test_fault_signal_with_marker_still_crashes(self, conn):
        """SIGSEGV is the worker's own bug — the marker must not excuse it."""
        tid = kb.create_task(conn, title="t", assignee="a")
        _running_worker(conn, tid, 81001)
        kbd.write_infra_drain_marker()
        kbd._recent_worker_exits[81001] = (11, time.time())
        assert kbd.detect_crashed_workers(conn) == [tid]
        run = _latest_run(conn, tid)
        assert run["outcome"] == "crashed"
        assert kb._json_dict(run["metadata"]).get("exit_kind") == "signaled"
        assert kb.get_task(conn, tid).consecutive_failures == 1


class TestInfraRespawnGuard:
    def test_infra_killed_run_guards_then_releases(self, conn, monkeypatch):
        monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
        tid = kb.create_task(conn, title="t", assignee="a")
        _running_worker(conn, tid, 82001)
        kbd.write_infra_drain_marker()
        kbd.detect_crashed_workers(conn)
        assert kbd.check_respawn_guard(conn, tid) == "infrastructure_cooldown"

        # Cooldown elapsed -> released EARLY (blocker_auth must not re-trap the
        # stamped drain text); the card respawns on a later tick.
        conn.execute(
            "UPDATE task_runs SET ended_at=? WHERE task_id=?",
            (int(time.time()) - 301, tid),
        )
        conn.commit()
        assert kbd.check_respawn_guard(conn, tid) is None

    def test_cooldown_zero_respawns_immediately(self, conn, monkeypatch):
        monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "0")
        tid = kb.create_task(conn, title="t", assignee="a")
        _running_worker(conn, tid, 83001)
        kbd.write_infra_drain_marker()
        kbd.detect_crashed_workers(conn)
        assert kbd.check_respawn_guard(conn, tid) is None


class TestDrainMarkerWriter:
    def test_writer_records_inflight_workers_only(self, conn):
        tid = kb.create_task(conn, title="t", assignee="a")
        _running_worker(conn, tid, 84001)
        other = kb.create_task(conn, title="idle", assignee="a")  # never claimed

        path = kbd.write_infra_drain_marker(reason="daemon_shutdown")
        marker = json.loads(Path(path).read_text(encoding="utf-8"))
        assert marker["reason"] == "daemon_shutdown"
        assert marker["host"] == kb._claimer_id().split(":", 1)[0]
        assert 0 < marker["written_at"] <= int(time.time())
        listed = {w["task_id"] for w in marker["workers"]}
        assert tid in listed and other not in listed
        entry = next(w for w in marker["workers"] if w["task_id"] == tid)
        assert entry["pid"] == 84001
        assert entry["worker_started_at"] == FP
        assert entry["board"] == "default"
        assert entry["run_id"] is not None

    def test_writer_is_best_effort(self, conn, monkeypatch):
        """A broken board enumeration must never raise out of a shutdown path."""
        def _boom(**kwargs):
            raise RuntimeError("board enumeration down")
        monkeypatch.setattr(kb, "list_boards", _boom)
        assert kbd.write_infra_drain_marker() is None


class TestDispatchSurface:
    def test_dispatch_once_surfaces_infra_killed(self, conn):
        tid = kb.create_task(conn, title="t", assignee="a")
        _running_worker(conn, tid, 85001)
        kbd.write_infra_drain_marker()

        res = kbd.dispatch_once(conn, dry_run=True)

        assert res.infra_killed == [tid]
        assert res.crashed == []
        assert res.auto_blocked == []
        assert "infra_killed=1" in kbd.describe_suppression([res])
