"""The confirmed-restart stamp settles the host update-restart obligation.

Root cause this pins: the obligation modeled "restart owed" durably but "restart done" only
as the ABSENCE of contrary evidence — no restart executor ever advanced the record, and the
only completion stamp went unread by the warning path. A clean ``hermes gateway restart``
onto a new PID at the same checkout therefore left the armed record standing, and every CLI
startup warned "previous update was not restarted" forever.

The corrected state machine (sentinel contract): restart executors record RESTART_IN_FLIGHT
for the stop→respawn window, the freshly loaded gateway stamps RESTARTED, and readers honour
the stamp before any live re-probe. Fail-closed branches stay: a stamp for a different SHA,
an expired in-flight window and an inventoried arm without a SHA still warn / refuse.
"""

from __future__ import annotations

import os
import time

import pytest

import hermes_cli.update_cmd_fleet as fleet
import hermes_cli.update_host_obligation as host_obligation

SHA = "a" * 40
OTHER_SHA = "b" * 40
CHECKOUT_SHA = "f" * 40  # a live checkout HEAD that is neither the record's nor a foreign stamp's


@pytest.fixture
def host_locks(tmp_path, monkeypatch):
    """One host state dir + one HERMES_HOME per test — the multiplex topology, isolated."""
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "gateway-locks"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    return tmp_path


def _arm(expected_sha: str = SHA, runtimes: list[dict] | None = None) -> None:
    if runtimes is None:
        runtimes = [{"kind": "gateway", "profile": "coder"}]
    assert fleet._write_fleet_restart_pending_marker(expected_sha=expected_sha, runtimes=runtimes)


def _no_live_probe(monkeypatch):
    """The live fleet probe and the checkout capture must not run: the stamp is the proof."""
    monkeypatch.setattr(
        "hermes_cli.update_receipt.collect_fleet_versions",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("live probe must not run")))
    monkeypatch.setattr(
        "hermes_cli.update_cmd._current_checkout_sha",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("checkout capture must not run")))


def test_stamped_restart_discharges_without_live_probe(host_locks, monkeypatch):
    _arm(SHA)
    _no_live_probe(monkeypatch)
    host_obligation.mark_host_restart_completed(
        SHA, pid=4242, start_time=123.5, confirmed=("gateway-boot",), by="gateway-boot")
    assert not fleet._update_owes_fleet_restart()
    assert not fleet._fleet_restart_obligation_armed()  # discharged, not just silenced
    assert not host_obligation.host_restart_in_flight()


def test_stamp_for_different_sha_proves_nothing(host_locks, monkeypatch):
    _arm(SHA)
    monkeypatch.setattr(
        "hermes_cli.update_receipt.collect_fleet_versions",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("live probe must not run")))
    # A foreign/legacy stamp for another code state (e.g. a takeover's own record shape).
    host_obligation.amend_host_obligation(restarted={"sha": OTHER_SHA, "pid": 1, "at": 1.0})
    assert fleet._update_owes_fleet_restart()
    assert fleet._fleet_restart_obligation_armed()


def test_in_flight_window_softens_the_startup_warning(host_locks, monkeypatch, capfd):
    _arm(SHA)
    assert host_obligation.begin_host_restart_flight(
        target_sha=SHA, old_pid=4242, old_start_time=111.0,
        watcher="detached-watcher", watch_budget_s=120)
    monkeypatch.setattr("hermes_cli.update_cmd._current_checkout_sha", lambda *a, **kw: CHECKOUT_SHA)
    monkeypatch.setattr("hermes_cli.update_cmd_fleet_checkout.checkout_contains", lambda sha: False)
    # An empty fleet probe would otherwise fail closed into the loud warning.
    monkeypatch.setattr("hermes_cli.update_receipt.collect_fleet_versions", lambda *a, **kw: [])
    fleet._warn_pending_fleet_restart_on_startup()
    err = capfd.readouterr().err
    assert "restart is in progress" in err
    assert "did not restart" not in err
    assert "cannot be verified" not in err


def test_expired_in_flight_window_keeps_the_warning(host_locks, monkeypatch, capfd):
    _arm(SHA)
    assert host_obligation.begin_host_restart_flight(
        target_sha=SHA, old_pid=4242, watcher="gateway-restart", watch_budget_s=120)
    host_obligation.amend_host_obligation(
        restart_in_flight={"target_sha": SHA, "old_pid": 4242, "watcher": "gateway-restart",
                           "at": time.time() - 300, "deadline": time.time() - 1})
    monkeypatch.setattr("hermes_cli.update_cmd._current_checkout_sha", lambda *a, **kw: CHECKOUT_SHA)
    monkeypatch.setattr("hermes_cli.update_cmd_fleet_checkout.checkout_contains", lambda sha: False)
    monkeypatch.setattr("hermes_cli.update_receipt.collect_fleet_versions", lambda *a, **kw: [])
    fleet._warn_pending_fleet_restart_on_startup()
    err = capfd.readouterr().err
    assert "restart is in progress" not in err
    assert "cannot be verified" in err  # the distinct §5.6 text, not the mixed-modules certainty


def test_failed_flight_then_stamp_still_settles(host_locks, monkeypatch):
    """An expired flight is not a scar: the confirmed successor stamp still discharges."""
    _arm(SHA)
    host_obligation.begin_host_restart_flight(target_sha=SHA, old_pid=4242, watcher="gateway-restart")
    host_obligation.amend_host_obligation(
        restart_in_flight={"target_sha": SHA, "old_pid": 4242, "watcher": "gateway-restart",
                           "at": time.time() - 300, "deadline": time.time() - 1})
    _no_live_probe(monkeypatch)
    host_obligation.mark_host_restart_completed(SHA, pid=5555, by="gateway-boot")
    assert not host_obligation.host_restart_in_flight()  # stamping clears the stale flight
    assert not fleet._update_owes_fleet_restart()
    assert not fleet._fleet_restart_obligation_armed()


def test_boot_stamp_records_the_running_process(host_locks, monkeypatch):
    _arm(SHA)
    monkeypatch.setattr("hermes_cli.version_info.get_code_identity", lambda *a, **kw: {"sha": SHA})
    monkeypatch.setattr("hermes_cli.process_identity._process_create_time", lambda *a, **kw: 777.5)
    assert host_obligation.stamp_gateway_boot_restart()
    stamp = host_obligation.read_host_obligation()["restarted"]
    assert stamp["sha"] == SHA and stamp["pid"] == os.getpid()
    assert stamp["start_time"] == 777.5 and stamp["by"] == "gateway-boot"
    assert host_obligation.host_restart_already_completed(SHA)
    assert host_obligation.host_restart_already_completed(SHA, pid=os.getpid())
    assert not host_obligation.host_restart_already_completed(SHA, pid=os.getpid() + 1)
    assert host_obligation.host_restart_already_completed(SHA, start_time=777.9)
    assert not host_obligation.host_restart_already_completed(SHA, start_time=779.0)


def test_boot_stamp_never_crosses_a_re_armed_sha(host_locks, monkeypatch):
    _arm(SHA)
    monkeypatch.setattr("hermes_cli.version_info.get_code_identity", lambda *a, **kw: {"sha": OTHER_SHA})
    monkeypatch.setattr("hermes_cli.process_identity._process_create_time", lambda *a, **kw: 777.5)
    # The record was re-armed for SHA; a gateway booted on OTHER_SHA is not that debt's proof.
    host_obligation.stamp_gateway_boot_restart()
    assert "restarted" not in host_obligation.read_host_obligation()
    assert fleet._fleet_restart_obligation_armed()


def test_inventoried_arm_without_sha_drops_the_inventory(host_locks):
    assert fleet._write_fleet_restart_pending_marker(
        expected_sha="", runtimes=[{"kind": "gateway", "profile": "coder"}])
    record = host_obligation.read_host_obligation()
    assert record["expected_sha"] == ""
    # The owed set is never stored unnamed: it names a debt no code identity could ever prove
    # current (#125952's permanent-warning class). The debt stays, dischargeable via the
    # fleet-current-on-checkout rule.
    assert "inventory" not in record


def test_flight_without_standing_record_is_a_no_op(host_locks):
    assert not host_obligation.begin_host_restart_flight(
        target_sha=SHA, old_pid=4242, watcher="gateway-restart")
    assert not host_obligation.host_restart_in_flight()
    assert not host_obligation.host_obligation_present()
