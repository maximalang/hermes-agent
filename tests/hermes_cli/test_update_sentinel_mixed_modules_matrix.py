"""Automated regression matrix for the update-restart sentinel warning (QA S1–S5).

Scenario source: the QA mixed-modules matrix + SENTINEL-STATE-CONTRACT §5. Every
scenario drives the REAL reader chain — ``collect_fleet_versions`` with only the
process-identity layers mocked (control socket, per-home live-pid resolver, code
identity) — so the fleet-row classification feeding the warning is exercised, not
stubbed. No live gateway, no network, no install/update.

Scenario map (test ids cite these; on the unfixed base the S3 / S2-flight /
S1-foreign-writer / S5-corrupt arms fail — the fix's contract — while the
S1-stale / S2-down / S4.2 / S5-stale arms are invariants that must hold on both):

- S1 stale sentinel warns           → test_s1_*
- S2 failed restart keeps warning   → test_s2_*
- S3 confirmed clean restart clears → test_s3_*
- S4 multiple profiles isolated     → test_s4_*
- S5 genuinely mixed stays closed   → test_s5_*
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

import hermes_cli.update_cmd_fleet as fleet
import hermes_cli.update_host_obligation as host_obligation

HEAD_SHA = "e" * 40   # the checkout the update pulled
OLD_SHA = "7" * 40    # pre-update code a live gateway may still serve
OTHER_SHA = "b" * 40  # a foreign checkout / second install
LIVE_PID = os.getpid()
DEAD_PID = 999999899  # never a live pid


@pytest.fixture
def host(tmp_path, monkeypatch):
    """One host: a shared gateway-lock state dir (one obligation record per install)
    plus the default profile's HERMES_HOME — the multiplex topology, per test."""
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "gateway-locks"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "default-home"))
    return tmp_path


class FleetTopology:
    """Real ``collect_fleet_versions`` over a mocked process-identity layer.

    ``sockets``/``live_pids`` are mutable per home so a test can publish or retract
    identity mid-scenario (S3's stop→respawn window). ``code_roots`` forces the
    external-checkout classification without touching psutil.
    """

    def __init__(self, monkeypatch, root: Path):
        self.root = root
        self.default_home = root / "default-home"
        self.default_home.mkdir(parents=True, exist_ok=True)  # _profile_homes lists it only if present
        self.profiles_root = root / "profiles"
        self.profiles_root.mkdir()
        self.sockets: dict[str, dict | None] = {}
        self.live_pids: dict[str, int | None] = {}
        self.code_roots: dict[int, Path | None] = {}
        monkeypatch.setattr(
            "hermes_cli.version_info.get_code_identity",
            lambda *a, **kw: {"sha": HEAD_SHA, "version": "9.9.9"})
        monkeypatch.setattr("hermes_cli.profiles._get_default_hermes_home", lambda: self.default_home)
        monkeypatch.setattr("hermes_cli.profiles._get_profiles_root", lambda: self.profiles_root)
        monkeypatch.setattr(
            "gateway.control_socket.identify_gateway",
            lambda home, **kw: self.sockets.get(str(home)))
        monkeypatch.setattr(
            "gateway.status.live_gateway_pid_for_home",
            lambda home: self.live_pids.get(str(home)))
        monkeypatch.setattr(
            "hermes_cli.update_receipt._gateway_code_root",
            lambda pid, home: self.code_roots.get(pid))

    def add_profile(self, name: str) -> Path:
        home = self.profiles_root / name
        home.mkdir(parents=True, exist_ok=True)
        return home

    def publish_socket(self, home: Path, pid: int, sha: str, served: list[str] | None = None) -> None:
        identity = {"pid": pid, "code_sha": sha, "code_version": "9.9.9"}
        if served is not None:
            identity["served_profiles"] = served
        self.sockets[str(home)] = identity

    def write_state(self, home: Path, record: dict) -> None:
        (home / "gateway_state.json").write_text(json.dumps(record), encoding="utf-8")

    def collect(self, **kwargs):
        from hermes_cli.update_receipt import collect_fleet_versions
        return collect_fleet_versions(**kwargs)


@pytest.fixture
def topo(monkeypatch, host):
    return FleetTopology(monkeypatch, host)


_DEFAULT_RUNTIMES = object()  # sentinel so _arm(runtimes=None) arms inventory-LESS


def _arm(expected_sha: str = HEAD_SHA, runtimes: list[dict] | None = _DEFAULT_RUNTIMES) -> Path:
    """Arm the HOST obligation (record A) and return its path."""
    if runtimes is _DEFAULT_RUNTIMES:
        runtimes = [{"kind": "gateway", "profile": "default"}]
    assert fleet._write_fleet_restart_pending_marker(expected_sha=expected_sha, runtimes=runtimes)
    return host_obligation.host_obligation_path()


def _startup_stderr(capsys) -> str:
    fleet._warn_pending_fleet_restart_on_startup()
    return capsys.readouterr().err


def _boot_stamp(sha: str) -> bool:
    """The freshly loaded gateway stamps the record it booted for (sentinel §5.2)."""
    from hermes_cli import version_info
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(version_info, "get_code_identity", lambda *a, **kw: {"sha": sha, "version": "9.9.9"})
        mp.setattr("hermes_cli.process_identity._process_create_time", lambda *a, **kw: 777.5)
        return host_obligation.stamp_gateway_boot_restart()


# ── S1 — stale sentinel warns ────────────────────────────────────────────────


def test_s1_stale_fleet_row_keeps_mixed_modules_warning(topo, host, capsys):
    """S1: a live gateway provably serving pre-update code keeps the exact
    mixed-modules warning (§1.1) and never touches the record."""
    record_path = _arm()
    topo.publish_socket(topo.default_home, pid=111, sha=OLD_SHA, served=["default"])

    assert fleet._update_owes_fleet_restart() is True
    err = _startup_stderr(capsys)
    assert "did not restart running gateways" in err
    assert "mixed sys.modules" in err
    assert "cannot be verified" not in err

    record = host_obligation.read_host_obligation()
    assert record["expected_sha"] == HEAD_SHA and "restarted" not in record
    assert record_path.is_file()


def test_s1_foreign_writer_state_file_warns_unverifiable(topo, host, capsys):
    """S1 (host form: cron-harness foreign writer): a ``gateway_state.json`` whose
    pid is live but NOT verified as the home's gateway must not classify from its
    self-reported SHA — §5.6 names the failure instead of claiming mixed modules."""
    record_path = _arm()
    topo.write_state(topo.default_home, {
        "pid": LIVE_PID, "gateway_state": "running", "kind": "hermes-gateway",
        "code_sha": OLD_SHA, "code_version": "0.21.3",
        "argv": ["profiles/tech/cache/scratch/harness_cron_events.py"],
    })
    rows = topo.collect()
    assert [row["state"] for row in rows] == ["unknown"] and rows[0]["code_sha"] is None

    assert fleet._update_owes_fleet_restart() is True
    err = _startup_stderr(capsys)
    assert "cannot be verified (a running gateway's identity could not be established)" in err
    assert "did not restart running gateways" not in err
    assert record_path.is_file() and "restarted" not in host_obligation.read_host_obligation()


def test_s1_dead_state_file_and_legacy_notice_marker_keep_debt(topo, host, capsys):
    """S1 (host form: stale 0.22.1 state + 0.21.3-era ``.restart_pending.json``): a
    dead pid's state file yields no row and no proof; the gateway's own planned-
    restart notice is inert for the obligation reader — the debt warning stands."""
    _arm()
    topo.write_state(topo.default_home, {
        "pid": DEAD_PID, "gateway_state": "running", "code_version": "0.22.1", "kind": "hermes-gateway"})
    (topo.default_home / ".restart_pending.json").write_text("{}", encoding="utf-8")

    assert topo.collect() == []  # no pre-restart snapshot: rollout-safe no-row
    assert fleet._update_owes_fleet_restart() is True
    err = _startup_stderr(capsys)
    assert "did not restart running gateways" in err and "⚠" in err


# ── S2 — failed restart keeps the warning ───────────────────────────────────


@pytest.mark.parametrize("gateway_state", ["running", "startup_failed"],
                         ids=["s2-down-row", "s2-startup-failed-no-row"])
def test_s2_gateway_never_came_back_keeps_the_debt(topo, host, capsys, gateway_state):
    """S2: the restart phase stopped the gateway and nothing replaced it — a `down`
    row fails the matrix; a startup_failed record yields no row at all, and absence
    never discharges an armed debt."""
    record_path = _arm()
    topo.write_state(topo.default_home, {
        "pid": DEAD_PID, "gateway_state": gateway_state, "kind": "hermes-gateway"})

    rows = topo.collect(pre_restart_pids=[DEAD_PID])
    expected_rows = 1 if gateway_state == "running" else 0
    assert len(rows) == expected_rows
    if rows:
        assert rows[0]["state"] == "down"

    assert fleet._update_owes_fleet_restart() is True  # reader probe: no row → debt stands
    assert "⚠" in _startup_stderr(capsys)
    assert record_path.is_file()


def test_s2_expired_flight_without_successor_keeps_warning_armed(topo, host, capsys):
    """S2 (fix's new subcase): the stop→respawn window expired with no stamped
    successor — the restart is treated as failed; the record stays armed and no
    in-progress line softens the warning (fail-closed preserved)."""
    record_path = _arm()
    assert host_obligation.begin_host_restart_flight(
        target_sha=HEAD_SHA, old_pid=4242, watcher="detached-watcher", watch_budget_s=120)
    host_obligation.amend_host_obligation(
        restart_in_flight={"target_sha": HEAD_SHA, "old_pid": 4242, "watcher": "detached-watcher",
                           "at": time.time() - 300, "deadline": time.time() - 1})

    assert fleet._update_owes_fleet_restart() is True
    err = _startup_stderr(capsys)
    assert "⚠" in err and "restart is in progress" not in err
    flight = host_obligation.host_restart_in_flight()
    assert flight is not None and flight["expired"] is True
    assert "restarted" not in host_obligation.read_host_obligation()
    assert record_path.is_file()


# ── S3 — confirmed clean restart clears the warning (the regression fix) ─────


def test_s3_boot_stamp_settles_warning_without_live_probe(topo, host, monkeypatch, capsys):
    """S3: the gateway that booted for the update stamps RESTARTED and the reader
    honours the stamp BEFORE any live re-probe — the false "previous update was not
    restarted" forever-warning is gone, the record is discharged on disk, and a
    second read is idempotent."""
    record_path = _arm()
    assert host_obligation.begin_host_restart_flight(
        target_sha=HEAD_SHA, old_pid=4242, watcher="gateway-restart")
    # The live probe must not even run: the stamp is the whole proof (§5.3).
    def _probe_must_not_run(*a, **kw):  # pragma: no cover - fails the test if reached
        raise AssertionError("live fleet probe must not run once the boot stamp stands")
    monkeypatch.setattr("hermes_cli.update_receipt.collect_fleet_versions", _probe_must_not_run)

    assert _boot_stamp(HEAD_SHA) is True
    assert fleet._update_owes_fleet_restart() is False
    assert _startup_stderr(capsys) == ""

    assert not record_path.is_file()                       # record A discharged on disk
    assert not fleet._fleet_restart_pending_marker_path().is_file()  # legacy marker B too
    assert fleet._update_owes_fleet_restart() is False     # idempotent re-read


def test_s3_socket_identity_discharge_still_works(topo, host, capsys):
    """S3 (old evidence route, invariant): a live multiplexer publishing the pulled
    SHA still discharges the debt through the fleet probe — the fix must not break
    the pre-existing positive-evidence path."""
    _arm(runtimes=[{"kind": "gateway", "profile": p} for p in ("default", "qa2")])
    topo.publish_socket(topo.default_home, pid=222, sha=HEAD_SHA, served=["default", "qa2"])

    assert fleet._update_owes_fleet_restart() is False
    assert _startup_stderr(capsys) == ""
    assert not host_obligation.host_obligation_path().is_file()


# ── S4 — multiple profiles stay isolated ─────────────────────────────────────


def test_s4_host_debt_seen_from_every_profile_and_settled_only_when_all_covered(topo, host, monkeypatch, capsys):
    """S4: the obligation is host-scoped, so a sibling profile's CLI sees the same
    debt; one profile's restart never erases another profile's inventory debt — only
    coverage of every owed profile discharges, for everyone at once."""
    qa2_home = topo.add_profile("qa2")
    _arm(runtimes=[{"kind": "gateway", "profile": p} for p in ("default", "qa2")])
    topo.publish_socket(topo.default_home, pid=333, sha=HEAD_SHA, served=["default"])
    monkeypatch.setenv("HERMES_HOME", str(qa2_home))  # the qa2 CLI

    assert fleet._fleet_restart_obligation_armed() is True      # host record, shared
    assert fleet._update_owes_fleet_restart() is True           # qa2 still uncovered
    assert "⚠" in _startup_stderr(capsys)
    assert host_obligation.host_obligation_path().is_file()

    topo.publish_socket(topo.default_home, pid=333, sha=HEAD_SHA, served=["default", "qa2"])
    assert fleet._update_owes_fleet_restart() is False          # multiplexer covers all owed
    assert _startup_stderr(capsys) == ""
    assert not host_obligation.host_obligation_path().is_file()
    monkeypatch.setenv("HERMES_HOME", str(topo.default_home))   # the default CLI agrees
    assert fleet._update_owes_fleet_restart() is False


def test_s4_two_installs_are_keyed_apart(topo, host, capsys):
    """S4 (review S3 isolation): a second install of the same OS user owns its own
    keyed record — its completed restart never erases this install's debt, and each
    install's boot stamp settles only its own record."""
    _arm(expected_sha=HEAD_SHA)
    original_path = host_obligation.host_obligation_path
    install_b = original_path().with_name("host-update-restart-deadbeefdeadbeef.json")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(host_obligation, "host_obligation_path", lambda: install_b)
        assert host_obligation.write_host_obligation(
            expected_sha=OTHER_SHA, runtimes=[{"kind": "gateway", "profile": "default"}])
        assert _boot_stamp(OTHER_SHA) is True                  # install B's gateway lands
        assert host_obligation.read_host_obligation()["restarted"]["sha"] == OTHER_SHA

    assert original_path().is_file()                            # install A's record untouched
    record = host_obligation.read_host_obligation()
    assert "restarted" not in record and record["expected_sha"] == HEAD_SHA
    assert fleet._update_owes_fleet_restart() is True           # A's debt survived B's restart
    assert "⚠" in _startup_stderr(capsys)


def test_s4_legacy_marker_of_another_profile_never_discharges_host_record(topo, host, monkeypatch, capsys):
    """S4.2: a per-home legacy breadcrumb (the pre-host-record writer's fallback)
    is visible to its arming profile only and can never discharge the host record
    another profile armed — but the shared gateway's coverage settles both."""
    qa2_home = topo.add_profile("qa2")
    record_path = _arm()
    # qa2's home carries a legacy marker whose empty inventory WOULD discharge if read.
    (qa2_home / "fleet_restart_pending").write_text(
        f"started={time.time()}\npid=4242\nexpected_sha={HEAD_SHA}\n"
        + 'inventory={"version": 1, "runtimes": []}\n', encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(qa2_home))

    assert fleet._fleet_restart_obligation_armed() is True      # host record governs
    assert fleet._update_owes_fleet_restart() is True
    assert "⚠" in _startup_stderr(capsys)
    assert record_path.is_file()

    topo.publish_socket(topo.default_home, pid=444, sha=HEAD_SHA, served=["default", "qa2"])
    assert fleet._update_owes_fleet_restart() is False
    assert not record_path.is_file()
    assert not (qa2_home / "fleet_restart_pending").is_file()   # breadcrumb swept on discharge


def test_s4_gatewayless_inventoryless_debt_discharges_on_idle_host(topo, host, monkeypatch, capsys):
    """S4 gatewayless branch (#118742): an inventory-less record on a host where no
    profile expects a gateway clears once HEAD still holds the pulled code; a home
    whose state file claims a gone-but-not-cleanly-stopped gateway keeps the debt."""
    _arm(runtimes=None)  # no inventory: the pre-inventory writer's shape
    monkeypatch.setattr(
        "hermes_cli.update_cmd_fleet_gatewayless.host_owes_no_gateway_restart", lambda: True)

    assert fleet._update_owes_fleet_restart() is False
    assert _startup_stderr(capsys) == ""
    assert not host_obligation.host_obligation_path().is_file()

    _arm(runtimes=None)
    monkeypatch.setattr(
        "hermes_cli.update_cmd_fleet_gatewayless.host_owes_no_gateway_restart", lambda: False)
    assert fleet._update_owes_fleet_restart() is True


# ── S5 — genuinely mixed module state stays fail-closed ──────────────────────


def test_s5_live_stale_gateway_with_foreign_stamp_still_warns(topo, host, capsys):
    """S5: a stamp for OTHER code proves nothing about this debt and must not mute
    the genuine mixed-modules warning a live stale row earns; the record is not
    cleared and the foreign stamp is not erased."""
    record_path = _arm()
    host_obligation.amend_host_obligation(restarted={"sha": OTHER_SHA, "pid": 1, "at": 1.0})
    topo.publish_socket(topo.default_home, pid=555, sha=OLD_SHA, served=["default"])

    assert fleet._update_owes_fleet_restart() is True
    err = _startup_stderr(capsys)
    assert "did not restart running gateways" in err and "mixed sys.modules" in err
    assert "cannot be verified" not in err
    record = host_obligation.read_host_obligation()
    assert record["restarted"]["sha"] == OTHER_SHA  # foreign stamp preserved, not promoted
    assert record_path.is_file()


@pytest.mark.parametrize("corrupt", [True, False], ids=["s5-corrupt-record", "s5-probe-failure"])
def test_s5_unreadable_terms_and_probe_failure_stay_fail_closed(topo, host, monkeypatch, capsys, corrupt):
    """S5 negative controls: a corrupt record is an obligation whose terms are
    unknown (never discharged — §5.6 names it), and a throwing fleet probe keeps
    the debt warning with the mixed-modules text."""
    record_path = _arm()
    if corrupt:
        record_path.write_bytes(b"{not json")
    else:
        monkeypatch.setattr(
            "hermes_cli.update_receipt.collect_fleet_versions",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("probe exploded")))

    assert fleet._fleet_restart_obligation_armed() is True
    assert fleet._update_owes_fleet_restart() is True
    err = _startup_stderr(capsys)
    assert "⚠" in err
    if corrupt:
        assert "cannot be verified (the pending-restart record is unreadable)" in err
    else:
        assert "did not restart running gateways" in err
    assert record_path.is_file()


# ── The three warning-text classes (sentinel contract §5.6) ─────────────────


def test_warning_text_honours_three_classes(topo, host, capsys):
    """§5.6 honesty: provable debt keeps the mixed-modules certainty; state that
    cannot be VERIFIED names the reason instead; a fresh stop→respawn window
    softens to an info line with no warning at all."""
    # Class 1 — genuine mixed modules.
    _arm()
    topo.publish_socket(topo.default_home, pid=666, sha=OLD_SHA, served=["default"])
    err = _startup_stderr(capsys)
    assert "mixed sys.modules" in err and "cannot be verified" not in err

    # Class 2 — unverifiable state (foreign-writer identity row).
    host_obligation.clear_host_obligation()
    _arm()
    topo.sockets.clear()
    topo.write_state(topo.default_home, {
        "pid": LIVE_PID, "gateway_state": "running", "code_sha": OLD_SHA, "kind": "hermes-gateway"})
    err = _startup_stderr(capsys)
    assert "cannot be verified (a running gateway's identity could not be established)" in err
    assert "did not restart running gateways" not in err

    # Class 3 — inside the stop→respawn window: info line only.
    host_obligation.clear_host_obligation()
    (topo.default_home / "gateway_state.json").unlink(missing_ok=True)
    _arm()
    assert host_obligation.begin_host_restart_flight(
        target_sha=HEAD_SHA, old_pid=4242, watcher="gateway-restart", watch_budget_s=120)
    err = _startup_stderr(capsys)
    assert "restart is in progress" in err and "⚠" not in err


# ── R6 classification probe — the row shapes every scenario above consumes ───


def test_r6_fleet_row_classification(topo, host):
    """The probe's classification feeding all scenarios: current (socket), stale
    (socket, old SHA), current via a verified fallback state file, unknown (foreign
    writer), down (dead pid the restart phase stopped), external (another checkout),
    and no row for a cleanly startup-failed record."""
    coder = topo.add_profile("coder")
    ops = topo.add_profile("ops")
    ops2 = topo.add_profile("ops2")
    topo.publish_socket(topo.default_home, pid=11, sha=HEAD_SHA, served=["default"])
    topo.publish_socket(coder, pid=12, sha=OLD_SHA)
    topo.live_pids[str(ops)] = LIVE_PID  # the resolver verifies this pid as ops's gateway
    topo.write_state(ops, {"pid": LIVE_PID, "gateway_state": "running", "code_sha": HEAD_SHA,
                           "kind": "hermes-gateway"})
    topo.write_state(ops2, {"pid": LIVE_PID, "gateway_state": "running",  # live pid, unverified
                            "code_sha": HEAD_SHA, "kind": "hermes-gateway"})
    dead_home = topo.add_profile("deadgw")
    topo.write_state(dead_home, {"pid": DEAD_PID, "gateway_state": "running", "kind": "hermes-gateway"})
    gone = topo.add_profile("gonegw")
    topo.write_state(gone, {"pid": 999999456, "gateway_state": "startup_failed", "kind": "hermes-gateway"})

    rows = topo.collect(pre_restart_pids=[DEAD_PID])
    by_profile = {row["profile"]: row for row in rows}
    assert by_profile["default"]["state"] == "current"
    assert by_profile["coder"]["state"] == "stale"
    assert by_profile["ops"]["state"] == "current"
    assert by_profile["ops2"]["state"] == "unknown" and by_profile["ops2"]["code_sha"] is None
    assert by_profile["deadgw"]["state"] == "down"
    assert "gonegw" not in by_profile  # startup_failed: rollout-safe no-row

    topo.code_roots[12] = Path("C:/srv/other-checkout")  # coder serves another checkout's code
    rows = topo.collect(pre_restart_pids=[DEAD_PID])
    assert next(r for r in rows if r["profile"] == "coder")["state"] == "external"
