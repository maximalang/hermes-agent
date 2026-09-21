"""Native 429 cooldown semantics for the zai pool (post owner-directive 21.09.2026).

The zai-specific reprobe branch is GONE. Behaviour now comes from the generic
machinery:

* a 429 WITHOUT a usable reset stamp is a short-window throttle -> stepped
  ladder (60s -> 5m -> 15m) via the persisted ``consecutive_429`` streak,
  instead of a flat hour bench. A working MAX key (quota window fine, only the
  RPM burst hit) re-enters rotation in a minute.
* a 429 WITH an absolute reset stamp parsed from the message ("will reset at
  2026-09-23 16:11:13", zai 1310 Weekly/Monthly credit) honours the provider
  stamp -> the dead key benches until the real reset instead of zombie-probing
  every 15 minutes.

Rotation on a multi-key pool still advances to the fallback immediately; the
ladder only sizes how long the FAILED entry stays benched.
"""

import time

from agent import credential_pool as cp


def _entry(entry_id: str, label: str, priority: int, token: str) -> cp.PooledCredential:
    return cp.PooledCredential(
        provider="zai",
        id=entry_id,
        label=label,
        auth_type="api_key",
        priority=priority,
        source="manual",
        access_token=token,
    )


def test_zai_1302_rotates_to_backup_when_pool_has_two_keys(monkeypatch):
    primary = _entry("max", "glm-2", 0, "key-max")
    backup = _entry("lite", "glm-old", 1, "key-lite")
    pool = cp.CredentialPool("zai", [primary, backup])
    monkeypatch.setattr(pool, "_persist", lambda *args, **kwargs: None)
    pool._current_id = primary.id

    selected = pool.mark_exhausted_and_rotate(
        status_code=429,
        error_context={"code": "1302", "message": "rate limited"},
        api_key_hint=primary.runtime_api_key,
        credential_id=primary.id,
    )

    assert selected is not None
    assert selected.id == backup.id


def test_zai_1302_stampless_ladder_starts_at_first_rung(monkeypatch):
    """A stamp-less 1302 (RPM burst) benches the failed key for 60s, not an hour."""
    primary = _entry("max", "glm-2", 0, "key-max")
    backup = _entry("lite", "glm-old", 1, "key-lite")
    pool = cp.CredentialPool("zai", [primary, backup])
    monkeypatch.setattr(pool, "_persist", lambda *args, **kwargs: None)
    pool._current_id = primary.id

    pool.mark_exhausted_and_rotate(
        status_code=429,
        error_context={"code": "1302", "message": "Rate limit reached for requests"},
        api_key_hint=primary.runtime_api_key,
        credential_id=primary.id,
    )

    marked = next(e for e in pool.entries() if e.id == primary.id)
    assert marked.consecutive_429 == 1
    bench = cp._exhausted_until(marked, sole_credential=False)
    assert bench is not None
    assert bench - time.time() <= cp.TRANSIENT_429_LADDER_SECONDS[0] + 5


def test_zai_stampless_429_ladder_escalates_then_caps(monkeypatch):
    """Repeated stamp-less 429s walk 60s -> 5m -> 15m and stay at the top rung."""
    primary = _entry("max", "glm-2", 0, "key-max")
    backup = _entry("lite", "glm-old", 1, "key-lite")
    pool = cp.CredentialPool("zai", [primary, backup])
    monkeypatch.setattr(pool, "_persist", lambda *args, **kwargs: None)
    pool._current_id = primary.id

    benches = []
    for _ in range(4):
        pool.mark_exhausted_and_rotate(
            status_code=429,
            error_context={"code": "1302", "message": "Rate limit reached for requests"},
            api_key_hint=primary.runtime_api_key,
            credential_id=primary.id,
        )
        marked = next(e for e in pool.entries() if e.id == primary.id)
        benches.append(cp._exhausted_until(marked, sole_credential=False) - marked.last_status_at)
        pool._current_id = primary.id  # force re-lease of the same key next round

    ladder = cp.TRANSIENT_429_LADDER_SECONDS
    assert benches[0] == ladder[0]
    assert benches[1] == ladder[1]
    assert benches[2] == ladder[2]
    assert benches[3] == ladder[2]  # capped at top rung


def test_zai_1310_absolute_stamp_honoured_until_real_reset(monkeypatch):
    """Weekly/Monthly credit exhaustion carries an absolute stamp: bench until it."""
    primary = _entry("max", "glm-2", 0, "key-max")
    backup = _entry("lite", "glm-old", 1, "key-lite")
    pool = cp.CredentialPool("zai", [primary, backup])
    monkeypatch.setattr(pool, "_persist", lambda *args, **kwargs: None)
    pool._current_id = backup.id

    reset_msg = "Weekly/Monthly Limit Exhausted. Your limit will reset at 2099-01-02 03:04:05"
    pool.mark_exhausted_and_rotate(
        status_code=429,
        error_context={"code": "1310", "message": reset_msg},
        api_key_hint=backup.runtime_api_key,
        credential_id=backup.id,
    )

    marked = next(e for e in pool.entries() if e.id == backup.id)
    assert marked.last_error_reset_at is not None
    # The streak does NOT apply to a stamped reset (not a short-window throttle).
    assert (marked.consecutive_429 or 0) == 0
    bench = cp._exhausted_until(marked, sole_credential=False)
    assert bench is not None
    # Benched to the real stamp (year 2099), i.e. way beyond any ladder rung.
    assert bench - time.time() > 3600


def test_zai_streak_resets_on_success(monkeypatch):
    """A success clears the streak so the next throttle restarts at 60s."""
    primary = _entry("max", "glm-2", 0, "key-max")
    pool = cp.CredentialPool("zai", [primary])
    monkeypatch.setattr(pool, "_persist", lambda *args, **kwargs: None)

    marked = primary
    for _ in range(3):
        marked = pool._mark_exhausted(
            marked, 429, {"code": "1302", "message": "rate limited"}, persist=False,
        )
    assert marked.consecutive_429 == 3

    recovered = pool._adopt(marked, persist=False, **cp._MARK_OK)
    assert recovered.consecutive_429 == 0
