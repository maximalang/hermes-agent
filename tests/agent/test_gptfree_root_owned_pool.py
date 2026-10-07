"""Regression: gpt-free pool rows are root-owned single-use grants (fleet-ops t_1083df20, defect F4).

A profile island resolves gpt-free rows only through the read_credential_pool
global-root fallback (the provider is root-owned; islands never run a device
flow for it). Before the fix gpt-free was missing from
SINGLE_USE_REFRESH_POOL_PROVIDERS, so any island write path — the lease-time
materialization, a wire-401 mark, or a refresh write-back — persisted a full
row copy into the ISLAND store, forking the single-use grant (incident
2026-10-06: company/qa profile stores grew exhausted forks of the production
row after wire 401s, masking the grant state for the root).

Fix under test:
  (a) gpt-free joins SINGLE_USE_REFRESH_POOL_PROVIDERS — island persists route
      update-only into the ROOT store under the root flock, fail-closed, and
      never create a profile slice;
  (b) a borrowed-row refresh transaction takes the ROOT auth flock
      (target_path = root auth.json), so two islands cannot double-spend the
      single-use refresh token.

Fixture shape mirrors tests/agent/test_credential_pool_profile_oauth_fork.py:
a real temp HERMES_HOME root plus real named profiles, real auth.json I/O,
no live stores, no network (the plugin refresh hook is stubbed).
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_constants
import agent.credential_pool as CP
from agent.credential_pool import load_pool
from hermes_cli.profiles import create_profile

PROVIDER = "gpt-free"
ROW_ID = "gfbarn01-test"

AT_1 = "at_root_1"
RT_1 = "rt_root_1"
AT_2 = "at_root_2"
RT_2 = "rt_root_2"


def _write_store(path: Path, pool_rows) -> None:
    path.write_text(
        json.dumps({"version": 1, "providers": {}, "credential_pool": {PROVIDER: pool_rows}}),
        encoding="utf-8",
    )


def _root_row(**over):
    row = {
        "id": ROW_ID,
        "label": "free-team",
        "auth_type": "oauth",
        "priority": 0,
        "source": "manual:device_code",
        "access_token": AT_1,
        "refresh_token": RT_1,
        "base_url": "https://api.openai.example/v1",
    }
    row.update(over)
    return row


def _pool_slice(store_path: Path):
    """Raw-slice inspection: the provider's rows in that store, or None when absent."""
    if not store_path.exists():
        return None
    data = json.loads(store_path.read_text(encoding="utf-8"))
    return (data.get("credential_pool") or {}).get(PROVIDER)


def _store_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    root = tmp_path / "hermes-root"
    root.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.setenv("HERMES_HOME", str(root))
    hermes_constants._default_hermes_root_memo = None
    _write_store(root / "auth.json", [_root_row()])

    def use(home: Path) -> None:
        monkeypatch.setenv("HERMES_HOME", str(home))
        hermes_constants._default_hermes_root_memo = None

    return SimpleNamespace(
        root=root,
        root_store=root / "auth.json",
        use=use,
        make_island=lambda name="forge": create_profile(name),
    )


def _spy_locks(monkeypatch):
    """Record the target_path of every _auth_store_lock taken in the pool module."""
    lock_calls = []
    real_lock = CP._auth_store_lock

    def spy(*args, **kwargs):
        lock_calls.append(kwargs.get("target_path"))
        return real_lock(*args, **kwargs)

    monkeypatch.setattr(CP, "_auth_store_lock", spy)
    return lock_calls


def _stub_refresh_hook(monkeypatch, calls):
    def fake_hook(entry):
        calls.append(entry.id)
        return {"access_token": AT_2, "refresh_token": RT_2, "expires_in": 3600}

    monkeypatch.setattr(
        CP,
        "plugin_refresh_hook",
        lambda provider: fake_hook if provider == PROVIDER else None,
    )


def test_island_wire_401_marks_root_only(fleet):
    """A 401 on a leased gpt-free row marks the ROOT row; the island never persists a slice."""
    island = fleet.make_island()
    fleet.use(island)
    island_store = island / "auth.json"

    pool = load_pool(PROVIDER)
    assert [e.id for e in pool._entries] == [ROW_ID]  # leased via the root fallback

    pool.mark_exhausted_and_rotate(status_code=401, error_context=None, credential_id=ROW_ID)

    assert _pool_slice(island_store) in (None, []), "island must never persist a gpt-free slice"
    assert RT_1 not in _store_text(island_store)
    root_rows = _pool_slice(fleet.root_store)
    assert root_rows and root_rows[0]["id"] == ROW_ID
    assert root_rows[0]["last_status"] == "exhausted"
    assert root_rows[0]["last_error_code"] == 401


def test_island_empty_slice_leases_root_row_without_writes(fleet):
    """The census-deadlock shape: an island with an EMPTY gpt-free slice still leases the root
    row through the fallback, and the lease itself writes nothing into the island store."""
    island = fleet.make_island()
    fleet.use(island)
    island_store = island / "auth.json"
    _write_store(island_store, [])  # empty slice — read_credential_pool falls back to root
    before = _store_text(island_store)

    pool = load_pool(PROVIDER)

    assert [e.id for e in pool._entries] == [ROW_ID]
    assert pool._entries[0].access_token == AT_1
    assert getattr(pool, "_borrowed_root_ids", set()) == {ROW_ID}
    assert _store_text(island_store) == before  # the lease is write-free
    assert _pool_slice(island_store) == []


def test_island_refresh_uses_root_flock_and_persists_root_only(fleet, monkeypatch):
    island = fleet.make_island()
    fleet.use(island)
    island_store = island / "auth.json"

    calls = []
    _stub_refresh_hook(monkeypatch, calls)
    pool = load_pool(PROVIDER)
    lock_calls = _spy_locks(monkeypatch)

    updated = pool.try_refresh_matching(credential_id=ROW_ID)

    assert updated is not None and updated.access_token == AT_2
    assert calls == [ROW_ID]  # exactly one refresh POST — no double-spend
    assert fleet.root_store in lock_calls  # the transaction serialized on the ROOT flock
    root_rows = _pool_slice(fleet.root_store)
    assert root_rows[0]["access_token"] == AT_2
    assert root_rows[0]["refresh_token"] == RT_2
    assert _pool_slice(island_store) in (None, [])
    assert AT_2 not in _store_text(island_store)


def test_root_process_refresh_unchanged(fleet, monkeypatch):
    """Control: on the owning root nothing changes — active-store lock, own-store persist."""
    fleet.use(fleet.root)

    calls = []
    _stub_refresh_hook(monkeypatch, calls)
    pool = load_pool(PROVIDER)
    lock_calls = _spy_locks(monkeypatch)

    updated = pool.try_refresh_matching(credential_id=ROW_ID)

    assert updated is not None and updated.access_token == AT_2
    assert calls == [ROW_ID]
    assert lock_calls and all(target is None for target in lock_calls)
    root_rows = _pool_slice(fleet.root_store)
    assert root_rows[0]["access_token"] == AT_2
    assert root_rows[0]["refresh_token"] == RT_2
