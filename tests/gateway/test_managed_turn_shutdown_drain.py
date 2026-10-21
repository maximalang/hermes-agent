"""R10: managed-worker turns are part of the bounded shutdown drain.

They run in child processes, outside ``_running_agents``: the drain must count them, the timed-out
path must send them the same Stop a user's Stop sends, and the final settlement wait must stay
bounded while keeping the admission's own fenced settlement (real subprocess workers)."""
import asyncio
import subprocess
import sys
from types import SimpleNamespace

import pytest

from gateway.session_contract import SessionRef
from tests.gateway.restart_test_helpers import make_restart_runner
from tests.waiting import async_wait_until


@pytest.mark.platforms("linux")
@pytest.mark.asyncio
async def test_drain_counts_managed_turns_and_the_timeout_path_stops_them(tmp_path):
    from gateway.session_managed_worker import ManagedWorker
    seen = tmp_path / 'controls'
    child = subprocess.Popen([sys.executable, '-c', 'import sys; open(sys.argv[1], "wb").write(sys.stdin.buffer.readline())',  # noqa: ASYNC220 -- Popen returns immediately; the test drives the child through its handle
                              str(seen)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    worker = ManagedWorker(child)
    worker.writer.start()
    runner, _adapter = make_restart_runner()
    runner.session_authority = SimpleNamespace(sessions={}, _managed_workers={'s': worker})
    try:
        assert runner._active_work_count() == 1
        _snapshot, timed_out = await runner._drain_active_agents(0)
        assert timed_out is True
        runner._interrupt_running_agents('Gateway shutting down')
        await asyncio.to_thread(child.wait, 10)
        assert seen.read_bytes() == b'{"type":"stop"}\n'
    finally:
        await asyncio.to_thread(worker.close)
    assert runner._active_work_count() == 0


@pytest.mark.platforms("linux")
def test_settlement_stops_a_silent_managed_worker_instead_of_waiting_unbounded(monkeypatch):
    """The reviewer's probe: a started managed admission whose child never answers. Settlement sends
    Stop, the turn settles interrupted through its own path, and nothing waits on HELLO_SECONDS."""
    from gateway import run_runtime
    from gateway import session_managed_worker as managed
    original = subprocess.Popen

    def silent_child(args, **kwargs):
        return original([sys.executable, '-c', 'import time\nwhile True: time.sleep(1)'], **kwargs)
    monkeypatch.setattr(managed.subprocess, 'Popen', silent_child)
    ref = SessionRef('profile', 's')
    live = SimpleNamespace(task=None, route='r', event_stream=SimpleNamespace(execution={}))
    authority = SimpleNamespace(profile_id='profile', pending_results={}, waiters={}, sessions={'s': live},
                                check_approval_generation=lambda *a: None, pending_stops={}, adopted={},
                                adopt_agent=lambda session_id, generation, worker: None)
    runner = SimpleNamespace(session_authority=authority)
    authority.runner = runner  # settle also hard-stops in-process turns through the runner
    row = {'admission_id': 'adm', 'principal_id': 'owner', 'generation': 3, 'payload': {'text': 'go'}}

    async def scenario():
        live.task = asyncio.create_task(managed.execute_managed(authority, ref, row, policy=None))
        await async_wait_until(lambda: 's' in getattr(authority, '_managed_workers', {}),
                               what='the managed worker to register')
        async with asyncio.timeout(15):
            await run_runtime.settle_gateway_runtime(runner)
        return live.task

    task = asyncio.run(scenario())
    assert task.done() and not task.cancelled() and task.result() == ''
    assert authority.pending_results['adm']['result']['interrupted'] is True
    assert authority._managed_workers == {}


@pytest.mark.platforms("linux")
def test_wedged_before_ready_settles_interrupted_within_the_ready_budget(monkeypatch):
    """2026-10-21 zombie class: a child that says hello, takes its bootstrap and then wedges
    mid-construction (numpy import stuck in a DLL load under memory pressure) must die inside
    ``MANAGED_READY_SECONDS`` and settle INTERRUPTED — never unknown (nothing executed), never
    parked on ``next_frame(None)`` forever while reclaim kills only the dispatcher submitter."""
    from gateway import session_managed_worker as managed

    original = subprocess.Popen

    def hello_then_silent_child(args, **kwargs):
        return original([sys.executable, '-c',
                         'import sys, time; sys.stdout.write(\'{"type": "hello"}\\n\'); '
                         'sys.stdout.flush()\nwhile True: time.sleep(1)'], **kwargs)
    monkeypatch.setattr(managed.subprocess, 'Popen', hello_then_silent_child)
    monkeypatch.setattr(managed, 'MANAGED_READY_SECONDS', 2)

    def fake_reserve(authority, *, admission_id, process, principal_id, hello):
        return {'pid': process.pid, 'birth': 'x', 'session_id': 's', 'execution_id': 'e', 'secret': None}
    monkeypatch.setattr(managed, 'reserve_admission_worker', fake_reserve)
    monkeypatch.setattr(managed, '_bootstrap',
                        lambda *a: {'type': 'bootstrap', 'api_key': None, 'policy': {}, 'scope': {}})
    # The loss bookkeeping needs the real session DB; this test pins the ready-budget branch's
    # settlement, not the ledger write (covered by the reservation shards).
    import gateway.session_worker_reservation as reservation
    monkeypatch.setattr(reservation, 'lose_admission_worker', lambda authority, row, scope: None)

    ref = SessionRef('profile', 's')
    live = SimpleNamespace(task=None, route='r', event_stream=SimpleNamespace(execution={}))
    authority = SimpleNamespace(profile_id='profile', pending_results={}, waiters={}, sessions={'s': live},
                                check_approval_generation=lambda *a: None, pending_stops={}, adopted={},
                                adopt_agent=lambda session_id, generation, worker: None)
    runner = SimpleNamespace(session_authority=authority)
    authority.runner = runner
    row = {'admission_id': 'adm', 'principal_id': 'owner', 'generation': 3, 'payload': {'text': 'go'}}

    async def scenario():
        live.task = asyncio.create_task(managed.execute_managed(authority, ref, row, policy=None))
        return await asyncio.wait_for(live.task, 30)

    assert asyncio.run(scenario()) == ''
    assert authority.pending_results['adm']['result']['interrupted'] is True
    assert authority.pending_results['adm']['result']['interrupt_message'] == 'managed_worker_ready_timeout'
    assert authority._managed_workers == {}
