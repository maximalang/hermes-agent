"""Locked retired-tree recovery: a committed publish or a completed rollback
must not be reported as failed solely because retired bytes are held open.

Regression for the 2026-09-30 Windows updater incident
(hermes-update-lock-20260930-224002.log):

* line 2520 — the git publish had committed, then the run exited 1 deleting
  ``tools/.previous-git-2.53.0+3-win32-x64/usr/bin/bash.exe`` (WinError 5);
* line 2531 — the retry died in settle deleting the same retired tree;
* line 2566 — source prep failed deleting a ``.displaced-*`` tree holding
  ``DLLs/libcrypto-3-x64.dll`` after the rollback itself had succeeded.

Conventions (test_pm_core / test_pm_authority): real loopback server, real
archives, real store — no mocked stores. Injected PermissionError wrappers
exercise the tolerance logic on every host; the ``platforms("windows")``
tests prove it against REAL kernel holds, per the task's acceptance recipes:
``ctypes.CreateFileW`` WITHOUT delete sharing (delete and rename refused)
and ``LoadLibrary`` on a copied test DLL shipped inside the fake archive —
the incident's shape, where the rename into ``.previous-`` succeeded with a
loaded image inside while the delete was refused with WinError 5.

These tests import only symbols that exist on the unmodified base so the
RED run fails on behavior (raised/masked errors), never on collection.
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
from contextlib import contextmanager
from pathlib import Path

import pytest

import pm.paths as paths
from pm.lock import Facts, Lockfile
from pm.package import InstallError
from pm.store import Store, current_target, tree_digest
from tests.pm._fixtures import make_tar, served as served  # noqa: F401
from tests.pm.test_pm_core import FakeTool, pm_env  # noqa: F401

STAGE_TARGET = "linux-arm64-bionic"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _route_target(route: str) -> str:
    return current_target() if route == "install" else STAGE_TARGET


def _entry(route_or_target: str = "install") -> Path:
    target = _route_target(route_or_target) if route_or_target in ("install", "stage") else route_or_target
    return paths.store_root() / FakeTool().store_entry("1.0", target)


def _realize(route: str):
    from pm.install import ensure, stage_only
    if route == "install":
        return ensure("faketool", explicit=True, base_env={})
    return stage_only("faketool", STAGE_TARGET)


def _repin(pm_env, archive_name: str, files: dict) -> str:
    """Publish a new same-version archive (different bytes/digest): the
    replacement flow that renames the live entry into .previous-<entry>."""
    lock_path, _, docroot, base_url = pm_env
    _, digest = make_tar(docroot, archive_name, files)
    lockfile = Lockfile(lock_path)
    lockfile.set_pin(
        "faketool", "1.0",
        {"any": {"url": f"{base_url}/{archive_name}", "sha256": digest}},
    )
    lockfile.save()
    return digest


def _assert_authoritative(route: str, digest: str) -> None:
    """The new bytes and the authoritative record agree, per route."""
    entry = _entry(route)
    assert (entry / "bin/faketool").exists()
    if route == "install":
        fact = Facts(paths.facts_path()).get("faketool")
        assert fact["artifacts"] == [digest]
        assert fact["digest"] == tree_digest(entry)
    else:
        marker = json.loads((entry / ".pm-stage-pin.json").read_text())
        assert marker["sha256"] == [digest]
        assert not paths.facts_path().exists()


def _store_dirs(*prefixes: str) -> list:
    root = paths.store_root()
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith(prefixes))


def _block_removal(monkeypatch, *prefixes: str) -> None:
    """Injected persistent hold: removing the named retired trees fails like
    a Windows share-mode refusal, on any host. Every other removal (fetch
    cache, scratch, failed stages) passes through to the real primitive.

    Signature-compatible with both the base ``_remove_entry(store, name)``
    and a candidate that adds keyword retry controls."""
    import pm.install as install_mod

    real = install_mod._remove_entry

    def guarded(store, entry_name, **kwargs):
        if entry_name.startswith(prefixes):
            raise PermissionError(13, "injected persistent hold", entry_name)
        return real(store, entry_name, **kwargs)

    monkeypatch.setattr(install_mod, "_remove_entry", guarded)


def _failing_record(monkeypatch, cause: str = "injected commit failure") -> None:
    def record(self, *args, **kwargs):
        raise InstallError("faketool", cause)
    monkeypatch.setattr(Facts, "record", record)


# --- real Windows kernel handles (acceptance item 6) -----------------------

_GENERIC_READ = 0x8000_0000
_FILE_SHARE_READ = 0x1
_FILE_SHARE_DELETE = 0x4
_OPEN_EXISTING = 3
_INVALID_HANDLE = ctypes.c_void_p(-1).value


def _kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    return kernel32


def _open_handle(path: Path, share: int):
    kernel32 = _kernel32()
    handle = kernel32.CreateFileW(str(path), _GENERIC_READ, share, None, _OPEN_EXISTING, 0, None)
    if handle is None or handle == _INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    return kernel32, handle


def _open_hard(path: Path):
    """CreateFileW WITHOUT delete sharing — the spec's recipe. Deletion and
    rename of the file (and of directories containing it) are refused
    (sharing violation, WinError 32) while the handle is open."""
    return _open_handle(path, _FILE_SHARE_READ)


_DLL_BYTES = None


def _dll_bytes() -> bytes:
    """A real loadable DLL for the image-hold recipe: a copy of this
    interpreter's own _ctypes.pyd (its only non-system import,
    python31x.dll, is already loaded in-process, so LoadLibrary maps the
    copy from wherever the test puts it)."""
    global _DLL_BYTES
    if _DLL_BYTES is None:
        import _ctypes
        _DLL_BYTES = Path(_ctypes.__file__).read_bytes()
    return _DLL_BYTES


def _make_bin_tar(docroot: Path, archive_name: str, entries: dict) -> str:
    """make_tar from _fixtures, extended to bytes members (a real DLL cannot
    ride through a str-only fixture)."""
    import hashlib
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for rel, content in entries.items():
            data = content.encode() if isinstance(content, str) else content
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(data))
    payload = buf.getvalue()
    (docroot / archive_name).write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def _repin_bin(pm_env, archive_name: str, entries: dict) -> str:
    lock_path, _, docroot, base_url = pm_env
    digest = _make_bin_tar(docroot, archive_name, entries)
    lockfile = Lockfile(lock_path)
    lockfile.set_pin(
        "faketool", "1.0",
        {"any": {"url": f"{base_url}/{archive_name}", "sha256": digest}},
    )
    lockfile.save()
    return digest


def _load_library(path: Path):
    kernel32 = _kernel32()
    kernel32.LoadLibraryW.restype = ctypes.c_void_p
    kernel32.LoadLibraryW.argtypes = [ctypes.c_wchar_p]
    kernel32.FreeLibrary.argtypes = [ctypes.c_void_p]
    kernel32.FreeLibrary.restype = ctypes.c_int
    module = kernel32.LoadLibraryW(str(path))
    if not module:
        raise ctypes.WinError(ctypes.get_last_error())
    return kernel32, module


@contextmanager
def dll_hold(path: Path):
    """REAL image-section hold: LoadLibrary on a copied test DLL (the spec's
    second recipe; the incident's shape — a loaded libcrypto / running
    bash.exe inside the retired tree). Ancestor directory renames stay
    ALLOWED, which is how the live tree became .previous-git-* in the first
    place, while deletion is refused with WinError 5 until FreeLibrary."""
    kernel32, module = _load_library(path)
    try:
        yield path
    finally:
        kernel32.FreeLibrary(module)


# ---------------------------------------------------------------------------
# injected-hold tests (every host): the tolerance contract
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("route", ["install", "stage"])
def test_committed_replacement_survives_locked_retired_tree(pm_env, monkeypatch, caplog, route):
    """Hypothesis 1: the replacement committed (facts / stage marker are
    authoritative), then reclaiming the retired tree hit a persistent hold.
    The operation must SUCCEED, keep the new bytes authoritative, retain the
    old bytes, and report the retention honestly — never claim deletion."""
    _repin(pm_env, "gen1.tar.gz", {"bin/faketool": "#!v1", "share/extra": "keep"})
    _realize(route)
    digest2 = _repin(pm_env, "gen2.tar.gz", {"bin/faketool": "#!v2"})

    _block_removal(monkeypatch, ".previous-")
    with caplog.at_level(logging.WARNING, logger="pm.install"):
        _realize(route)

    _assert_authoritative(route, digest2)
    assert (_entry(route) / "bin/faketool").read_bytes() == b"#!v2"
    retained = _store_dirs(".previous-")
    assert retained, "the locked retired tree must be retained, not silently dropped"
    assert (retained[0] / "share/extra").read_bytes() == b"keep", \
        "retained bytes stay recoverable and are NOT claimed deleted"
    assert any("retain" in r.getMessage().lower() and str(retained[0]) in r.getMessage()
               for r in caplog.records), "the retained tree must be reported by path"


@pytest.mark.parametrize("route", ["install", "stage"])
def test_second_install_while_hold_persists_is_idempotent(pm_env, monkeypatch, route):
    """The updater retries while the hold persists. The retry must be safe
    and outcome-idempotent: authoritative state unchanged, retained tree
    still present, no repeated-failure loop (incident line 2531: the retry
    died in settle on the same retired tree)."""
    _repin(pm_env, "gen1.tar.gz", {"bin/faketool": "#!v1"})
    _realize(route)
    digest2 = _repin(pm_env, "gen2.tar.gz", {"bin/faketool": "#!v2"})

    _block_removal(monkeypatch, ".previous-", ".displaced-")
    _realize(route)
    _assert_authoritative(route, digest2)

    entry = _entry(route)
    facts_before = paths.facts_path().read_bytes() if paths.facts_path().exists() else None
    entry_before = tree_digest(entry)
    _realize(route)  # the retry, hold still active
    _assert_authoritative(route, digest2)
    assert tree_digest(entry) == entry_before
    assert (paths.facts_path().read_bytes() if paths.facts_path().exists() else None) == facts_before
    assert _store_dirs(".previous-"), "retention persists until the hold releases"


def test_replacement_while_retired_tree_held_republishes_and_reclassifies(pm_env, monkeypatch):
    """A held retired tree must not wedge the NEXT replacement either: once
    facts are committed and the realized entry verifies, the retired tree is
    proven garbage. If it cannot be deleted, it must at least leave the
    deterministic .previous- restore-point slot (renamed into the
    .displaced- garbage class) so the following publish can proceed. The
    rename destroys nothing — the bytes wait for the sweep."""
    _repin(pm_env, "gen1.tar.gz", {"bin/faketool": "#!v1"})
    _realize("install")
    digest2 = _repin(pm_env, "gen2.tar.gz", {"bin/faketool": "#!v2"})

    _block_removal(monkeypatch, ".previous-")
    _realize("install")
    retained = _store_dirs(".previous-")
    assert len(retained) == 1

    digest3 = _repin(pm_env, "gen3.tar.gz", {"bin/faketool": "#!v3"})
    _realize("install")  # settle must tolerate the held tree and free the slot
    _assert_authoritative("install", digest3)
    assert (_entry() / "bin/faketool").read_bytes() == b"#!v3"
    displaced = _store_dirs(".displaced-")
    assert displaced, "the held gen1 tree must survive as reclassified garbage"
    assert (displaced[0] / "bin/faketool").read_bytes() == b"#!v1"


def test_locked_rollback_preserves_original_commit_error(pm_env, monkeypatch):
    """Hypothesis 2: the rollback SUCCEEDED (previous bytes restored, facts
    untouched), then deleting the displaced tree hit the hold. The caller
    must see the ORIGINAL commit failure, not the cleanup error, and the
    displaced bytes must be retained (incident line 2566)."""
    _repin(pm_env, "gen1.tar.gz", {"bin/faketool": "#!v1"})
    _realize("install")
    facts_before = paths.facts_path().read_bytes()
    _repin(pm_env, "gen2.tar.gz", {"bin/faketool": "#!v2"})

    _failing_record(monkeypatch)
    _block_removal(monkeypatch, ".displaced-")
    with pytest.raises(InstallError, match="injected commit failure"):
        _realize("install")

    assert (_entry() / "bin/faketool").read_bytes() == b"#!v1", "rollback restored the previous bytes"
    assert paths.facts_path().read_bytes() == facts_before, "facts stay at the last committed state"
    displaced = _store_dirs(".displaced-")
    assert displaced, "the locked displaced tree is retained, not claimed deleted"
    assert (displaced[0] / "bin/faketool").read_bytes() == b"#!v2"


def test_locked_rollback_preserves_original_verify_error_stage(pm_env, monkeypatch):
    """Stage-only semantics for the same guarantee: no facts commit exists,
    the authoritative marker rides inside the entry. A published-entry
    verification failure must roll back and report the VERIFICATION reason —
    the cleanup hold must not mask it — and the restored marker stays
    authoritative."""
    digest1 = _repin(pm_env, "gen1.tar.gz", {"bin/faketool": "#!v1"})
    _realize("stage")
    _repin(pm_env, "gen2.tar.gz", {"bin/faketool": "#!v2"})

    entry = _entry("stage")
    original_publish = Store.publish

    def corrupting_publish(self, staged, name):
        published = original_publish(self, staged, name)
        if name == entry.name:
            (published / "bin/faketool").unlink()  # published-verify failure
        return published

    monkeypatch.setattr(Store, "publish", corrupting_publish)
    _block_removal(monkeypatch, ".displaced-")
    with pytest.raises(InstallError, match="failed verification"):
        _realize("stage")

    marker = json.loads((entry / ".pm-stage-pin.json").read_text())
    assert marker["sha256"] == [digest1], "the restored gen1 marker is authoritative again"
    assert (entry / "bin/faketool").read_bytes() == b"#!v1"
    assert not paths.facts_path().exists()
    assert _store_dirs(".displaced-"), "the failed gen2 bytes are retained, not claimed deleted"


def test_committed_replacement_survives_locked_download_cache(pm_env, monkeypatch):
    """Same bug class one layer out: the fetch-<sha> archive cache is
    post-commit garbage (gc drops it, the next install retries). A hold on
    it must not fail the committed replacement either."""
    _repin(pm_env, "gen1.tar.gz", {"bin/faketool": "#!v1"})
    _realize("install")
    digest2 = _repin(pm_env, "gen2.tar.gz", {"bin/faketool": "#!v2"})

    _block_removal(monkeypatch, "fetch-")
    _realize("install")
    _assert_authoritative("install", digest2)
    assert _store_dirs("fetch-"), "the locked download cache is retained for gc / next install"


def test_retained_displaced_is_swept_once_the_hold_releases(pm_env):
    """Retention is bounded: once the hold releases, the NEXT install
    reclaims the retained displaced garbage on its own — no permanent leak
    and no manual step (gc deliberately preserves dot-dirs)."""
    _repin(pm_env, "gen1.tar.gz", {"bin/faketool": "#!v1"})
    _realize("install")
    _repin(pm_env, "gen2.tar.gz", {"bin/faketool": "#!v2"})

    # A dedicated MonkeyPatch instance: undoing the injected hold must not
    # tear down the pm_env fixture's own patches (same-fixture trap).
    hold = pytest.MonkeyPatch()
    try:
        _failing_record(hold)
        _block_removal(hold, ".displaced-")
        with pytest.raises(InstallError, match="injected commit failure"):
            _realize("install")
        assert _store_dirs(".displaced-")
    finally:
        hold.undo()  # the hold releases and the injected fault is repaired

    digest3 = _repin(pm_env, "gen3.tar.gz", {"bin/faketool": "#!v3"})
    _realize("install")
    _assert_authoritative("install", digest3)
    assert not _store_dirs(".displaced-"), "released garbage must be reclaimed by the next install"


# ---------------------------------------------------------------------------
# REAL Windows holds (acceptance item 6): kernel handles, not mocks
# ---------------------------------------------------------------------------

@pytest.mark.platforms("windows")
def test_real_dll_hold_publish_upgrade_and_release(pm_env, caplog):
    """End-to-end on a REAL image hold: a copied test DLL (the incident's
    libcrypto shape) ships inside the live tree and is LoadLibrary'd. The
    rename into .previous- stays allowed, deletion is refused with the raw
    WinError 5. The committed publish succeeds and reports the retention; a
    further replacement while the hold persists frees the restore-point slot
    by reclassifying the proven garbage; after release the next install
    reclaims everything (bounded)."""
    digest1 = _repin_bin(pm_env, "gen1.tar.gz",
                         {"bin/faketool": "#!v1", "vendor/hold.dll": _dll_bytes()})
    _realize("install")
    entry = _entry()
    previous = entry.with_name(".previous-" + entry.name)
    held_dll = entry / "vendor" / "hold.dll"
    digest2 = _repin(pm_env, "gen2.tar.gz", {"bin/faketool": "#!v2"})

    with dll_hold(held_dll):
        # Kernel-level control: deleting a loaded image is refused with the
        # incident's raw WinError 5 (not an injected stand-in).
        with pytest.raises(PermissionError) as denied:
            os.unlink(held_dll)
        assert denied.value.winerror == 5

        with caplog.at_level(logging.WARNING, logger="pm.install"):
            _realize("install")  # renames the live entry aside, DLL loaded
        _assert_authoritative("install", digest2)
        assert (previous / "vendor/hold.dll").is_file(), \
            "real hold: the retired tree survives the refused reclaim"
        assert any(str(previous) in r.getMessage() and "retain" in r.getMessage().lower()
                   for r in caplog.records), "retention is reported by path"

        digest3 = _repin(pm_env, "gen3.tar.gz", {"bin/faketool": "#!v3"})
        _realize("install")  # another changed pin while the hold persists
        _assert_authoritative("install", digest3)
        assert not previous.exists(), "proven garbage must leave the restore-point slot"
        displaced = _store_dirs(".displaced-")
        assert displaced and (displaced[0] / "vendor/hold.dll").is_file(), \
            "the reclassified tree, still holding the loaded DLL, waits for release"

    digest4 = _repin(pm_env, "gen4.tar.gz", {"bin/faketool": "#!v4"})
    _realize("install")
    _assert_authoritative("install", digest4)
    assert not _store_dirs(".displaced-", ".previous-"), "release enables bounded reclamation"


@pytest.mark.platforms("windows")
def test_real_hard_hold_committed_publish_retry_and_release(pm_env, monkeypatch, caplog):
    """The spec's exact recipe: CreateFileW WITHOUT delete sharing, attached
    to a file inside the retired tree right after the facts commit — delete
    AND rename are refused while the handle is open (raw sharing violation,
    WinError 32; the running-image WinError 5 shape is covered by the
    LoadLibrary tests). The committed replacement still succeeds, the
    retention is reported, a retry while held is safe/idempotent, and
    release reclaims."""
    digest1 = _repin(pm_env, "gen1.tar.gz", {"bin/faketool": "#!v1", "share/spare": "spare"})
    _realize("install")
    entry = _entry()
    previous = entry.with_name(".previous-" + entry.name)
    held = previous / "share/spare"
    digest2 = _repin(pm_env, "gen2.tar.gz", {"bin/faketool": "#!v2"})

    real_record = Facts.record
    state = {}

    def record_then_hold(self, *args, **kwargs):
        result = real_record(self, *args, **kwargs)
        if "handle" not in state:
            # One-shot: the publish already renamed the live entry aside;
            # attach the hard hold to the retired tree like a still-running
            # old binary would. Later records (the release phase) pass
            # through — the retired tree they see is a different generation.
            state["kernel32"], state["handle"] = _open_hard(held)
        return result

    monkeypatch.setattr(Facts, "record", record_then_hold)
    try:
        with caplog.at_level(logging.WARNING, logger="pm.install"):
            _realize("install")
        assert "handle" in state, "the real hold was attached after the commit"
        _assert_authoritative("install", digest2)
        assert (entry / "bin/faketool").read_bytes() == b"#!v2"
        assert previous.is_dir() and held.read_bytes() == b"spare"
        assert any(str(previous) in r.getMessage() and "retain" in r.getMessage().lower()
                   for r in caplog.records)

        # Kernel-level control: the hold really refuses deletion and rename.
        with pytest.raises(PermissionError) as denied:
            os.unlink(held)
        assert denied.value.winerror == 32, "raw sharing violation from the real handle"
        with pytest.raises(OSError):
            previous.rename(previous.with_name(".probe-moved"))
        assert previous.is_dir()

        # Retry while held: safe and idempotent (incident line 2531 died here).
        facts_bytes = paths.facts_path().read_bytes()
        entry_digest = tree_digest(entry)
        _realize("install")
        assert paths.facts_path().read_bytes() == facts_bytes
        assert tree_digest(entry) == entry_digest
        assert previous.is_dir(), "still retained — never claimed deleted"
    finally:
        if "handle" in state:
            state["kernel32"].CloseHandle(state["handle"])

    digest3 = _repin(pm_env, "gen3.tar.gz", {"bin/faketool": "#!v3"})
    _realize("install")
    _assert_authoritative("install", digest3)
    assert not previous.exists()
    assert not _store_dirs(".previous-", ".displaced-")


@pytest.mark.platforms("windows")
def test_real_dll_hold_rollback_preserves_original_error(pm_env, monkeypatch):
    """REAL held rollback (incident line 2566: a displaced tree holding
    DLLs/libcrypto-3-x64.dll): the commit fails while a loaded DLL sits in
    the freshly published bytes. The restore renames must succeed (image
    semantics), the ORIGINAL commit error must reach the caller unwrapped,
    the displaced bytes must be retained, and the next install after release
    sweeps them."""
    digest1 = _repin(pm_env, "gen1.tar.gz", {"bin/faketool": "#!v1"})
    _realize("install")
    entry = _entry()
    facts_before = paths.facts_path().read_bytes()
    digest2 = _repin_bin(pm_env, "gen2.tar.gz",
                         {"bin/faketool": "#!v2", "vendor/hold.dll": _dll_bytes()})

    state = {}
    real_record = Facts.record

    def record_hold_and_fail(self, *args, **kwargs):
        if "module" in state:
            # One-shot: later records (the release phase) pass through — the
            # gen3 tree carries no DLL and must commit normally.
            return real_record(self, *args, **kwargs)
        state["kernel32"], state["module"] = _load_library(entry / "vendor/hold.dll")
        raise InstallError("faketool", "injected commit failure")

    monkeypatch.setattr(Facts, "record", record_hold_and_fail)
    try:
        with pytest.raises(InstallError, match="injected commit failure"):
            _realize("install")
        assert (entry / "bin/faketool").read_bytes() == b"#!v1", "rollback restored gen1"
        assert paths.facts_path().read_bytes() == facts_before, "facts untouched"
        displaced = _store_dirs(".displaced-")
        assert displaced and (displaced[0] / "vendor/hold.dll").is_file(), \
            "the held displaced tree is retained, not claimed deleted"
    finally:
        if "module" in state:
            state["kernel32"].FreeLibrary(state["module"])

    digest3 = _repin(pm_env, "gen3.tar.gz", {"bin/faketool": "#!v3"})
    _realize("install")
    _assert_authoritative("install", digest3)
    assert not _store_dirs(".displaced-"), "released garbage is reclaimed by the next install"
