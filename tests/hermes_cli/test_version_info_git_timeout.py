"""A git that hangs past its timeout must not wedge version resolution.

Regression for the kanban-worker bootstrap wedge (card t_32d98f7f, QA runs
5578/5587): ``subprocess.run(..., timeout=3)`` kills git on expiry, but its
recovery path then calls ``communicate()`` with no timeout — on Windows that
joins pipe reader threads which stay blocked while ANY descendant of git holds
the inherited write ends (fsmonitor daemon, hook, credential helper), so the
worker's main thread hangs in ``init_agent`` forever.

The probe fakes exactly that shape in a fresh interpreter: ``git`` resolves to
a python shim whose ``rev-parse`` spawns a pipe-holder grandchild and then
hangs. ``_run_git`` must return ``None`` within a hard bound (kill + bounded
grace reap, then abandon the daemon reader threads), never wedge.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# How long the fake git child + holder stay alive past the probe. The test
# kills them in teardown anyway; this bounds orphans if teardown cannot.
_SHIM_TTL_S = 30

_SHIM_REV_PARSE = (
    "import os, subprocess, sys, time\n"
    "here = os.path.dirname(os.path.abspath(__file__))\n"
    "# Grandchild pipe-holder: inherits stdout/stderr and outlives the direct\n"
    "# child, so TerminateProcess(git) cannot close the pipe write ends.\n"
    "subprocess.Popen([sys.executable, os.path.join(here, 'holder.py')])\n"
    f"deadline = time.monotonic() + {_SHIM_TTL_S}\n"
    "while time.monotonic() < deadline:\n"
    "    time.sleep(0.5)\n"
)

_SHIM_HOLDER = (
    "import os, time\n"
    "here = os.path.dirname(os.path.abspath(__file__))\n"
    "with open(os.path.join(here, 'holder.pid'), 'a') as f:\n"
    "    f.write(str(os.getpid()) + '\\n')\n"
    f"deadline = time.monotonic() + {_SHIM_TTL_S}\n"
    "while time.monotonic() < deadline:\n"
    "    time.sleep(0.5)\n"
)

_PROBE_CODE = (
    "import json, sys, time\n"
    "from pathlib import Path\n"
    "from hermes_cli import version_info\n"
    "t0 = time.monotonic()\n"
    "result = version_info._run_git(Path(sys.argv[1]), 'rev-parse', 'HEAD')\n"
    "print(json.dumps({'elapsed': round(time.monotonic() - t0, 2), 'result': result}))\n"
)


def _kill_holder(bindir: Path) -> None:
    pidfile = bindir / "holder.pid"
    if not pidfile.exists():
        return
    for pid in filter(None, pidfile.read_text(encoding="utf-8").split()):
        subprocess.run(
            ["taskkill", "/F", "/PID", pid], capture_output=True, check=False,
        )


@pytest.mark.platforms("windows")
def test_run_git_returns_within_bound_when_a_git_child_holds_the_pipes(tmp_path):
    # On Windows Popen(["git", ...]) resolves only git.exe (CreateProcess
    # appends .exe), so the shim is a python.exe copy that executes the
    # argument-named script "rev-parse" from its working directory.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shutil.copyfile(sys.executable, bindir / "git.exe")
    (bindir / "rev-parse").write_text(_SHIM_REV_PARSE, encoding="utf-8")
    (bindir / "holder.py").write_text(_SHIM_HOLDER, encoding="utf-8")

    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
        "PYTHONPATH": str(ROOT),
    }
    try:
        out = subprocess.run(
            [sys.executable, "-c", _PROBE_CODE, str(bindir)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            env=env,
            cwd=str(bindir),
            check=False,
            stdin=subprocess.DEVNULL,
        )
    finally:
        _kill_holder(bindir)

    assert out.returncode == 0, out.stderr[-2000:]
    payload = json.loads(out.stdout.strip().splitlines()[-1])
    # Timed-out git yields no provenance, never an exception...
    assert payload["result"] is None
    # ...and never a wedge: the whole probe is bounded by the 3 s timeout plus
    # the bounded grace reap (asserted well under the 30 s subprocess cap).
    assert payload["elapsed"] <= 5.0, payload
