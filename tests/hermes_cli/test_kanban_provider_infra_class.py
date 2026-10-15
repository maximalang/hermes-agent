"""A worker that dies at startup on a provider/model *availability* wall is not a
card defect — and the board must be able to tell that apart from a real failure.

The sweep books such a death as the neutral ``infra`` outcome: no
``consecutive_failures`` increment, no ``gave_up``, no phantom failure row, and
a stored ``last_failure_error`` that is the provider's own words (ANSI-stripped)
rather than a scraped TUI frame. Only a rail that stays unavailable for
``_INFRA_RAIL_MAX_CONSECUTIVE`` spawns in a row parks the card, with an explicit
infra marker for the company lane.

Environment note: the provider wall surfaces as exit 1, 75 or 78 depending on
where it was noticed, so classification keys off the worker's own log text, not
the raw code. Every case below therefore drives the REAL reclaim path
(``detect_crashed_workers`` over a dead PID) with a realistic worker log.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER

# Byte-identical to what the 2026-10-14 loop class (t_fcc571ae, t_8e3623e9,
# t_c8fa0f13, t_3a302dee) left in its worker logs on provider=custom.
_UNAVAILABLE_FRAME = (
    "\x1b[2J\x1b[H\x1b[1;31m╭───────────────────────────╮\x1b[0m\n"
    "\x1b[1;31m│\x1b[0m ☤ Hermes                  \x1b[1;31m│\x1b[0m\n"
    "\x1b[1;31m╰───────────────────────────╯\x1b[0m\n"
    "session_id: 20261014_231501_dead\n"
    "Query: do the thing\n"
    "Error: model qwen-3.8-max is not available on this provider right now.\n"
)


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    kbd._recent_worker_exits.clear()
    kb.init_db()
    return home


def _dead_worker(conn, tid: str, pid: int, log_text: str, rc: int = 1) -> None:
    """Claim ``tid`` for a worker that already exited ``rc`` and wrote ``log_text``."""
    host = kb._claimer_id().split(":", 1)[0]
    kb.claim_task(conn, tid, claimer=f"{host}:w{pid}")
    conn.execute(
        "UPDATE tasks SET worker_pid=?, worker_started_at=NULL, started_at=? WHERE id=?",
        (pid, int(time.time()) - 120, tid),
    )
    conn.commit()
    log = kb.worker_log_path(tid)
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        f.write(f"{log_text}\n{KANBAN_WORKER_EXIT_TRAILER}{rc}\n")


def _last_run(conn, tid: str):
    return conn.execute(
        "SELECT outcome, error, metadata FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (tid,),
    ).fetchone()


# ---------------------------------------------------------------------------
# (a) provider-unavailable exit -> infra class, counters unchanged, cooldown park
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rc", [1, kb.KANBAN_RATE_LIMIT_EXIT_CODE, kb.KANBAN_TERMINAL_PROVIDER_EXIT_CODE])
def test_provider_unavailable_exit_is_infra_and_never_charges_the_card(kanban_home, rc):
    """The same wall surfaces as 1, 75 or 78 — all three must book as ``infra``.

    ``consecutive_failures`` stays at 0, the card is NOT parked as
    ``gave_up``/``blocked`` on the first death, and the run records the infra
    class so the streak counter can see it.
    """
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="rail is walled", assignee="a")
        _dead_worker(conn, tid, 70001, _UNAVAILABLE_FRAME, rc=rc)

        kbd.detect_crashed_workers(conn)

        task = kb.get_task(conn, tid)
        run = _last_run(conn, tid)
        assert run["outcome"] == "infra", f"rc={rc} must classify as infra"
        assert task.consecutive_failures == 0
        assert task.status == "ready"
        assert task.block_kind is None
        md = kb._json_dict(run["metadata"])
        assert md.get("infra") is True
        assert md.get("infra_reason") == "provider_unavailable"
        # A terminal-provider exit is unhealable in the ordinary path; the infra
        # reclassification must clear that marker or the breaker would trip.
        assert md.get("terminal_provider") is None
        # Neutral bookkeeping: the runtime bucket is reported, not the crash bucket.
        assert tid in kbd.detect_crashed_workers._last_infra
        assert tid not in kbd.detect_crashed_workers._last_auto_blocked


def test_infra_death_requeues_for_free_until_the_streak_threshold(kanban_home, monkeypatch):
    """Below the threshold an infra death is free; at the threshold the card parks
    with the infra marker instead of respawning forever."""
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "0")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="permanently walled", assignee="a")
        parked = []
        for i in range(kbd._INFRA_RAIL_MAX_CONSECUTIVE):
            _dead_worker(conn, tid, 71000 + i, _UNAVAILABLE_FRAME)
            kbd.detect_crashed_workers(conn)
            kb.recompute_ready(conn, failure_limit=10)
            parked.append(
                conn.execute("SELECT status, block_kind FROM tasks WHERE id=?", (tid,)).fetchone()["status"]
            )

        # Free requeues until the threshold, then exactly one sticky park.
        assert parked[:-1] == ["ready"] * (kbd._INFRA_RAIL_MAX_CONSECUTIVE - 1), parked
        row = conn.execute(
            "SELECT status, block_kind, consecutive_failures FROM tasks WHERE id=?", (tid,)
        ).fetchone()
        assert row["status"] == "blocked"
        assert row["block_kind"] == "transient"
        assert row["consecutive_failures"] == 0, "no attempt ran, so no card defect to count"
        assert kb.get_task(conn, tid).status == "blocked"

        # The park carries the infra marker for the company lane.
        ev = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid,)
        ).fetchone()
        assert ev["kind"] == "infra_parked"
        assert kb._json_dict(ev["payload"]).get("infra") is True

        # And it is STICKY: recompute_ready must not promote it right back.
        kb.recompute_ready(conn, failure_limit=10)
        assert kb.get_task(conn, tid).status == "blocked"
        # An operator releases it through the ordinary unblock path.
        kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).status == "ready"


def test_infra_cooldown_spaces_retries_without_tripping_blocker_auth(kanban_home, monkeypatch):
    """The stamped diagnostic legitimately contains provider words ("quota",
    "rate limit"); the guard must return the infra cooldown, never ``blocker_auth``."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="walled", assignee="a")
        _dead_worker(conn, tid, 72000, _UNAVAILABLE_FRAME)
        kbd.detect_crashed_workers(conn)

        monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
        assert kbd.check_respawn_guard(conn, tid) == "infra_cooldown"

        # Once the cooldown is off, the card must be released for retry — not
        # trapped by blocker_auth on the stamped provider text.
        monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "0")
        assert kbd.check_respawn_guard(conn, tid) is None


# ---------------------------------------------------------------------------
# (b) ANSI banner never becomes the stored reason
# ---------------------------------------------------------------------------


def test_stored_reason_is_the_provider_diagnostic_never_the_scraped_tui_frame(kanban_home):
    """``last_failure_error`` carries the provider's own words, ANSI-stripped —
    never a raw escape blob and never the rendered panel chrome."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="walled", assignee="a")
        _dead_worker(conn, tid, 73000, _UNAVAILABLE_FRAME)
        kbd.detect_crashed_workers(conn)

        stored = conn.execute(
            "SELECT last_failure_error FROM tasks WHERE id=?", (tid,)
        ).fetchone()["last_failure_error"]
        assert stored, "an infra death must still store a diagnostic"
        assert "not available" in stored
        assert "\x1b" not in stored, "no escape bytes in a board-visible reason"
        assert "╭" not in stored and "│" not in stored, "no TUI panel chrome"
        assert "session_id:" not in stored
        assert "Query: do the thing" not in stored
        # The run row carries the same sanitized text.
        assert "\x1b" not in (_last_run(conn, tid)["error"] or "")


# ---------------------------------------------------------------------------
# (c) genuine worker errors still count normally
# ---------------------------------------------------------------------------


def test_genuine_worker_error_still_counts_normally(kanban_home):
    """A real startup failure (missing profile launcher) is a card failure: the
    infra classifier must not swallow it."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="really broken", assignee="a")
        _dead_worker(conn, tid, 74000, "Traceback (most recent call last):\nRuntimeError: no such profile")
        kbd.detect_crashed_workers(conn)

        run = _last_run(conn, tid)
        assert run["outcome"] == "crashed"
        assert kb.get_task(conn, tid).consecutive_failures == 1
        assert kb._json_dict(run["metadata"]).get("infra") is None


def test_quota_window_signature_is_infra_not_a_crash(kanban_home):
    """A provider-scoped quota reset belongs to the rail, not the card."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="quota", assignee="a")
        _dead_worker(
            conn, tid, 75000,
            "API error: rate limit exceeded for this provider. limit resets at 12:00 UTC.",
            rc=kb.KANBAN_RATE_LIMIT_EXIT_CODE,
        )
        kbd.detect_crashed_workers(conn)

        run = _last_run(conn, tid)
        assert run["outcome"] == "infra"
        assert kb._json_dict(run["metadata"]).get("infra_reason") == "quota_window"
        assert kb.get_task(conn, tid).consecutive_failures == 0


# ---------------------------------------------------------------------------
# (d) 3 consecutive infra outcomes -> infra park marker
# ---------------------------------------------------------------------------


def test_three_consecutive_infra_outcomes_park_with_the_infra_marker(kanban_home, monkeypatch):
    """The bounded-respawn contract: exactly ``_INFRA_RAIL_MAX_CONSECUTIVE``
    consecutive infra outcomes park the card, and the park is releasable."""
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "0")
    assert kbd._INFRA_RAIL_MAX_CONSECUTIVE == 3
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="loop", assignee="a")
        streaks = []
        for i in range(3):
            _dead_worker(conn, tid, 76000 + i, _UNAVAILABLE_FRAME)
            kbd.detect_crashed_workers(conn)
            # Read the streak before the park run is appended: the park itself
            # writes a ``blocked`` run, which by definition ends the streak.
            streaks.append(kbd._consecutive_infra_runs(conn, tid))

        assert streaks[:2] == [1, 2], streaks
        # The third infra death crosses the threshold and parks in the SAME
        # sweep, which appends the ``blocked`` run — hence the streak reads 0
        # once the park has landed (the streak by definition ended).
        assert streaks[2] == 0, streaks
        assert kb.get_task(conn, tid).status == "blocked"

        kinds = [
            r["kind"] for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (tid,)
            )
        ]
        assert kinds.count("infra_parked") == 1, kinds
        assert "gave_up" not in kinds, "an unavailable rail must never read as a card give-up"

        # The provider's words are stored once, not once per retry.
        stored = conn.execute(
            "SELECT last_failure_error FROM tasks WHERE id=?", (tid,)
        ).fetchone()["last_failure_error"]
        assert stored.count("not available") == 1, stored

        # No phantom failure row either — the whole point of the class.
        assert conn.execute("SELECT consecutive_failures FROM tasks WHERE id=?", (tid,)).fetchone()[0] == 0