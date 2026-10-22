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


def test_ready_timeout_preserves_real_unknown_admission_and_cleans_child(tmp_path, monkeypatch):
    """A missing ready frame bounds startup but cannot prove no effects occurred.

    Use the real reservation/loss ledger: do not advertise interrupted while the
    stored unknown admission still fences its queued follower.
    """
    from contextlib import closing
    import threading

    from hermes_state import SessionDB
    from hermes_state_runtime import (
        RuntimeStoreError, admit_session_input, begin_runtime_epoch, claim_session_input,
    )
    from gateway import session_managed_worker as managed

    original = subprocess.Popen
    children, notices = [], []

    class HelloThenSilentChild(original):
        def __init__(self, args, **kwargs):
            super().__init__([sys.executable, '-c',
                'import os,json,psutil,time; p=psutil.Process(); '
                'print(json.dumps({"type":"hello","pid":os.getpid(),'
                '"birth":p.create_time(),"ancestors":[os.getppid()]}),flush=True); '
                'time.sleep(120)'], **kwargs)
            children.append(self)

    monkeypatch.setattr(managed.subprocess, 'Popen', HelloThenSilentChild)
    monkeypatch.setattr(managed, '_worker_env', lambda authority: None)
    monkeypatch.setattr(managed, 'MANAGED_READY_SECONDS', 2)
    monkeypatch.setattr(managed, '_bootstrap',
                        lambda *a: {'type': 'bootstrap', 'api_key': None, 'scope': {}})
    with closing(SessionDB(tmp_path / 'state.db')) as db:
        db.create_session('s', 'cli')
        epoch = begin_runtime_epoch(db, instance_id='owner')
        admit_session_input(db, epoch=epoch, principal_id='human', session_id='s',
                            request_id='input', payload={'text': 'go'})
        row = claim_session_input(db, epoch=epoch, session_id='s')
        follower = admit_session_input(db, epoch=epoch, principal_id='human', session_id='s',
                            request_id='follower', payload={'text': 'next'})
        stream = SimpleNamespace(execution={}, lock=threading.RLock(),
                                 publish=lambda *args: notices.append(args))
        live = SimpleNamespace(task=None, route='r', event_stream=stream,
                               controls=SimpleNamespace(snapshot=lambda *args: None))
        authority = SimpleNamespace(db=db, epoch=epoch, profile_id=str(tmp_path),
            pending_results={}, waiters={}, sessions={'s': live}, executing={'s': row['generation']},
            _require_admission_open=lambda: None, _publish_pending=lambda *args: None,
            check_approval_generation=lambda *args: None, pending_stops={}, adopted={},
            adopt_agent=lambda *args: None)
        authority.runner = SimpleNamespace(session_authority=authority)
        ref = SessionRef(str(tmp_path), 's')

        async def scenario():
            live.task = asyncio.create_task(managed.execute_managed(authority, ref, row, policy=None))
            return await asyncio.wait_for(live.task, 30)

        try:
            with pytest.raises(managed.ManagedExecutionUnknown, match='managed_worker_unknown'):
                asyncio.run(scenario())
            stored = db._read_one('SELECT status,outcome FROM session_admissions WHERE admission_id=?',
                                   (row['admission_id'],))
            assert tuple(stored) == ('unknown', None)
            assert db._read_one('SELECT status FROM worker_executions')[0] == 'terminal'
            with pytest.raises(RuntimeStoreError, match='unknown_execution'):
                claim_session_input(db, epoch=epoch, session_id='s')
            assert db._read_one('SELECT status FROM session_admissions WHERE admission_id=?',
                               (follower['admission_id'],))[0] == 'queued'
            assert row['admission_id'] not in authority.pending_results
            assert notices and notices[-1][1]['outcome'] == 'unknown'
            assert authority._managed_workers == {}
            assert children and all(child.poll() is not None for child in children)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)
