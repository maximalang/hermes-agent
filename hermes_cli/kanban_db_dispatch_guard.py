"""Respawn-guard decision helpers (sibling of ``kanban_db_dispatch``).

``check_respawn_guard`` itself stays in the dispatch module (tests patch it
there and its lane/requeue logic reads the dispatch tables); this sibling
owns the ``blocker_auth`` decision so the dispatch file — already over the
FILE_LINES ratchet cap — only shrinks. Imported late inside the guard per the
sibling contract; never a module-level cycle.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Optional

# Patterns in last_failure_error that indicate a quota / auth blocker.
# These errors won't resolve by retrying immediately — auto-block instead.
# The auth family is a curated list, not an open `auth\w*` stem: that stem
# also matched ordinary English words like "author"/"authored"/"authoring"/
# "authoritative" in worker progress prose, parking a healthy card forever
# (#117009).
_RESPAWN_BLOCKER_RE = re.compile(
    r"\b(quota|rate[\s_\-]?limit|429|403|"
    r"auth|authenticat(?:e|es|ed|ing|ion)|authoriz(?:e|es|ed|ing|ation)|"
    r"authoris(?:e|es|ed|ing|ation)|authz|"
    r"unauthorized|forbidden|billing|subscription|"
    r"access[\s_]denied|permission[\s_]denied|"
    r"invalid[\s_]api[\s_]key)\b",
    re.IGNORECASE,
)

# Outcomes whose failure paths stamp ``tasks.last_failure_error``: the
# rate-limited requeue and every ``_record_task_failure`` caller (timed_out /
# crashed / spawn_failed). Lifecycle outcomes — worker self-blocks (``blocked``),
# breaker-adjacent auto-blocks (``gave_up``), review handoffs, reclaims — end a
# run WITHOUT stamping, and the promote/unblock/assign paths clear the stamp.
_LFE_STAMPING_OUTCOMES = ("rate_limited", "crashed", "spawn_failed", "timed_out")


def _blocker_auth_hold(
    conn: sqlite3.Connection,
    task_id: str,
    err: Optional[str],
    latest_run: Optional[sqlite3.Row],
) -> bool:
    """True when the lossy ``last_failure_error`` text ``err`` should hold the
    respawn as ``blocker_auth``.

    Two bounds, both load-bearing. A latest ``crashed`` run never holds: a
    crash's persisted error is the worker's captured output — context, not a
    diagnosis — and may name benign commands such as ``claude auth status``
    (#117097). And the regex match is bound to FRESHNESS: it is actionable
    only while the run that stamped the error is the card's latest ended run
    (the stampers are ``_LFE_STAMPING_OUTCOMES``). A later ended run with any
    other outcome ended without re-stamping, and ``_clear_failure_counter``
    runs only on ``complete_task`` success — so holding on the old text parked
    cards forever: no cooldown, no failure counting, no breaker trip, every
    dispatcher tick re-held the spawn. Same-``ended_at`` ties stay held (fail
    closed, like the PR-guard tie); no stamping run at all stays held too
    (fail closed for stamps written outside an ended run).
    """
    if not err or (latest_run is not None and latest_run["outcome"] == "crashed"):
        return False
    if not _RESPAWN_BLOCKER_RE.search(err):
        return False
    stamping = conn.execute(
        "SELECT ended_at FROM task_runs WHERE task_id = ? AND ended_at IS NOT NULL "
        "AND outcome IN ('rate_limited', 'crashed', 'spawn_failed', 'timed_out') "
        "ORDER BY ended_at DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return (stamping is None or latest_run is None
            or stamping["ended_at"] == latest_run["ended_at"])
