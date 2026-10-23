"""Managed worker boundary: a worker FOR another profile never inherits the launch profile's
credentials, and a ledger id never names a path outside worker-outboxes (PR #106742 security lane)."""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("multiplex", [True, False])
def test_secondary_profile_worker_gets_no_launch_credentials(tmp_path, monkeypatch, multiplex):
    launch = tmp_path / ".hermes"
    sec = launch / "profiles" / "l106742sec"
    sec.mkdir(parents=True)
    (launch / ".env").write_text("OPENROUTER_API_KEY=LAUNCH-or\nMY_SERVICE_TOKEN=LAUNCH-mysvc\n")
    (sec / ".env").write_text("OPENROUTER_API_KEY=SEC-or\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(launch))
    for key, value in {"OPENROUTER_API_KEY": "LAUNCH-or", "MY_SERVICE_TOKEN": "LAUNCH-mysvc",
                       "NOUS_API_KEY": "LAUNCH-nous", "VAULT_ONLY_TOKEN": "LAUNCH-vault"}.items():
        monkeypatch.setenv(key, value)
    from hermes_cli import env_loader
    monkeypatch.setitem(env_loader._SECRET_SOURCES, "VAULT_ONLY_TOKEN", "bitwarden")
    from agent import secret_scope
    from gateway.session_managed_worker import _worker_env
    secret_scope.set_multiplex_active(multiplex)
    try:
        env = _worker_env(SimpleNamespace(profile_id=str(sec)))
        own = _worker_env(SimpleNamespace(profile_id=str(launch)))
    finally:
        secret_scope.set_multiplex_active(False)
    # The scrub follows the worker's OWNING profile, not the process-wide multiplex flag.
    assert env is not None and env["OPENROUTER_API_KEY"] == "SEC-or"
    assert sorted(k for k, v in env.items() if "LAUNCH" in str(v)) == []
    if not multiplex:
        assert own is None  # single-profile: the launch profile's own worker inherits byte-for-byte


def test_launch_profile_worker_keeps_env_only_tool_credentials_under_multiplex(tmp_path, monkeypatch):
    """Under multiplex the launch profile's own worker resolves what its owner resolves for that
    profile (``launch_secret_scope``): a tool key injected only into the launch environment
    (systemd ``Environment=``, ``op run``) reaches it, while a secondary's worker never sees it."""
    launch = tmp_path / ".hermes"
    sec = launch / "profiles" / "l106742sec"
    sec.mkdir(parents=True)
    (launch / ".env").write_text("OPENROUTER_API_KEY=LAUNCH-or\n")
    (sec / ".env").write_text("OPENROUTER_API_KEY=SEC-or\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setenv("FIRECRAWL_API_KEY", "ENVONLY-fc")
    monkeypatch.setenv("AUXILIARY_VISION_API_KEY", "ENVONLY-aux")
    from agent.secret_scope import get_secret, set_multiplex_active
    from gateway.session_managed_worker import _worker_env
    from tui_gateway.launch_profile_policy import activate_multi_profile_hosting, launch_profile_runtime_scope
    activate_multi_profile_hosting()
    try:
        with launch_profile_runtime_scope(launch):
            assert get_secret("FIRECRAWL_API_KEY") == "ENVONLY-fc"  # what the owner resolves
        own = _worker_env(SimpleNamespace(profile_id=str(launch)))
        other = _worker_env(SimpleNamespace(profile_id=str(sec)))
    finally:
        set_multiplex_active(False)
    assert own["FIRECRAWL_API_KEY"] == "ENVONLY-fc" and own["OPENROUTER_API_KEY"] == "LAUNCH-or"
    assert "AUXILIARY_VISION_API_KEY" not in own  # Hermes-internal secrets never reach a child
    assert "FIRECRAWL_API_KEY" not in other and other["OPENROUTER_API_KEY"] == "SEC-or"
    assert sorted(k for k, v in other.items() if "ENVONLY" in str(v) or "LAUNCH" in str(v)) == []


@pytest.mark.parametrize("bad", ["../../../../escape", "x/../../y", "..\\..\\win", "C:\\evil", ""])
def test_outbox_dir_only_names_paths_inside_worker_outboxes(tmp_path, bad):
    from agent.managed_worker import outbox_dir
    assert outbox_dir(tmp_path, "admission-worker:" + "ab" * 16).parent == tmp_path / "worker-outboxes"
    with pytest.raises(ValueError):
        outbox_dir(tmp_path, "admission-worker:" + bad if bad else bad)


def _managed_worker_import_probe(tmp_path, extra_env=None, stub_numpy=True):
    """Import agent.managed_worker in a clean subprocess; a stub numpy on PYTHONPATH
    records the thread count and BLAS env AT the moment the prewarm import runs."""
    import json
    import os
    import subprocess
    import sys

    repo_root = str(Path(__file__).resolve().parents[2])
    env = dict(os.environ)
    env["PYTHONPATH"] = str(tmp_path) + os.pathsep + repo_root
    env.pop("OPENBLAS_NUM_THREADS", None)
    if extra_env:
        env.update(extra_env)
    probe = tmp_path / "probe.json"
    if stub_numpy:
        (tmp_path / "numpy").mkdir(exist_ok=True)
        (tmp_path / "numpy" / "__init__.py").write_text(
            "import json, os, threading\n"
            f"open(r'{probe}', 'w').write(json.dumps({{\n"
            "    'threads': threading.active_count(),\n"
            "    'blas': os.environ.get('OPENBLAS_NUM_THREADS')}))\n",
            encoding="utf-8",
        )
    else:
        env["PYTHONPATH"] = repo_root  # real site-packages resolution, no stub
    out = subprocess.run(
        [sys.executable, "-c",
         "import agent, agent.managed_worker as mw; print('PROVENANCE', mw.__file__); print('UNDER', agent.__file__)"],
        capture_output=True, text=True, cwd=repo_root, env=env, timeout=180, check=False,
    )
    return out, probe


def test_worker_imports_numpy_in_module_body_before_any_reader_thread(tmp_path):
    """t_d4350193: the OpenBLAS DLL init wedged every bootstrap when numpy was first
    imported from inside the plugin loader (after the worker's reader threads exist).
    The prewarm must run in the module body — single-threaded, before any reader —
    and the BLAS pin must already be set when numpy loads."""
    import json

    out, probe = _managed_worker_import_probe(tmp_path)
    assert out.returncode == 0, out.stderr[-2000:]
    provenance = next(l for l in out.stdout.splitlines() if l.startswith("PROVENANCE"))
    repo_root = str(Path(__file__).resolve().parents[2])
    assert Path(provenance.split(" ", 1)[1].strip()).resolve() == (
        Path(repo_root) / "agent" / "managed_worker.py"
    ).resolve(), provenance
    data = json.loads(probe.read_text(encoding="utf-8"))
    assert data["blas"] == "1"  # pin applied before the prewarm import
    assert data["threads"] == 1  # module body: no reader threads started yet


def test_worker_prewarm_preserves_operator_blas_setting(tmp_path):
    import json

    out, probe = _managed_worker_import_probe(tmp_path, {"OPENBLAS_NUM_THREADS": "8"})
    assert out.returncode == 0, out.stderr[-2000:]
    assert json.loads(probe.read_text(encoding="utf-8"))["blas"] == "8"


def test_real_numpy_is_warm_after_managed_worker_import(tmp_path):
    """In an environment that has numpy (the managed worker env does), importing the
    worker module must leave it in sys.modules, so the holographic plugin's later
    import reuses it and never loads the DLL under reader-thread contention."""
    import os
    import subprocess
    import sys

    repo_root = str(Path(__file__).resolve().parents[2])
    env = dict(os.environ)
    env.pop("OPENBLAS_NUM_THREADS", None)
    have = subprocess.run(
        [sys.executable, "-c", "import importlib.util as u; print(u.find_spec('numpy') is not None)"],
        capture_output=True, text=True, env=env, timeout=60, check=False,
    )
    if have.returncode == 0 and have.stdout.strip() == "False":
        pytest.skip("numpy not installed in this environment")
    assert have.returncode == 0 and have.stdout.strip() == "True", have.stderr[-500:]
    out = subprocess.run(
        [sys.executable, "-c",
         "import agent, agent.managed_worker as mw, sys\n"
         "from pathlib import Path\n"
         "print('PROV', Path(mw.__file__).resolve())\n"
         "print('numpy' in sys.modules)"],
        capture_output=True, text=True, cwd=repo_root, env=env, timeout=180, check=False,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    prov = next(l for l in out.stdout.splitlines() if l.startswith("PROV "))
    assert Path(prov[len("PROV "):].strip()).resolve() == (
        Path(repo_root) / "agent" / "managed_worker.py"
    ).resolve(), prov
    assert out.stdout.strip().splitlines()[-1] == "True"


def _prewarm_list_probe(tmp_path, extra_env=None, stubs=None):
    """Import agent.managed_worker in a clean subprocess with a configured prewarm list.
    Each stub module appends {name, threads} to a jsonl probe the moment the prewarm
    imports it, recording the order and the thread count AT import time. The child also
    prints which of the stub names ended up in sys.modules."""
    import json
    import subprocess
    import sys

    repo_root = str(Path(__file__).resolve().parents[2])
    env = dict(os.environ)
    env["PYTHONPATH"] = str(tmp_path) + os.pathsep + repo_root
    env.pop("OPENBLAS_NUM_THREADS", None)
    if extra_env:
        env.update(extra_env)
    probe = tmp_path / "prewarm.jsonl"
    stubs = stubs or {}
    for name, body in stubs.items():
        mod = tmp_path / name
        mod.mkdir(exist_ok=True)
        if body is None:  # plain record-and-return stub
            body = ""
        # %-formatting (not f-strings): the dict braces below are literal source the
        # child compiles, and %r quotes the Windows probe path with its backslashes.
        stub_src = (
            "import json, threading\n"
            "open(%r, 'a').write(json.dumps({'name': %r, "
            "'threads': threading.active_count()}) + '\\n')\n" % (str(probe), name)
        ) + body
        (mod / "__init__.py").write_text(stub_src, encoding="utf-8")
    watched = "(" + ",".join(repr(n) for n in stubs) + ")" if stubs else "()"
    out = subprocess.run(
        [sys.executable, "-c",
         "import agent, agent.managed_worker as mw, sys\n"
         "from pathlib import Path\n"
         "print('PROVENANCE', mw.__file__)\n"
         f"print('MODULES', sorted(m for m in {watched} if m in sys.modules))\n"],
        capture_output=True, text=True, cwd=repo_root, env=env, timeout=180, check=False,
    )
    records = []
    if probe.exists():
        records = [json.loads(line) for line in probe.read_text(encoding="utf-8").splitlines() if line]
    return out, records


def _assert_probe_provenance(out):
    provenance = next(l for l in out.stdout.splitlines() if l.startswith("PROVENANCE"))
    repo_root = str(Path(__file__).resolve().parents[2])
    assert Path(provenance.split(" ", 1)[1].strip()).resolve() == (
        Path(repo_root) / "agent" / "managed_worker.py"
    ).resolve(), provenance
    modules = next(l for l in out.stdout.splitlines() if l.startswith("MODULES "))
    return [m for m in modules[len("MODULES "):].strip("[]'\"").replace("'", "").split(", ") if m]


def test_worker_prewarm_uses_configured_module_list(tmp_path):
    """t_dc7c5148: HERMES_WORKER_PREWARM_MODULES replaces the default numpy entry —
    exactly the configured modules warm, in declaration order, in the module body."""
    from agent.managed_worker import PREWARM_MODULES_ENV

    out, records = _prewarm_list_probe(
        tmp_path,
        extra_env={PREWARM_MODULES_ENV: "alpha, beta"},
        stubs={"alpha": None, "beta": None, "numpy": None},
    )
    assert out.returncode == 0, out.stderr[-2000:]
    modules = _assert_probe_provenance(out)
    assert [r["name"] for r in records] == ["alpha", "beta"]  # declaration order
    assert all(r["threads"] == 1 for r in records)  # module body: before any reader thread
    assert modules == ["alpha", "beta"]  # numpy stayed cold: the list replaces, not extends


def test_worker_prewarm_empty_env_disables_prewarm(tmp_path):
    from agent.managed_worker import PREWARM_MODULES_ENV

    out, records = _prewarm_list_probe(
        tmp_path, extra_env={PREWARM_MODULES_ENV: ""}, stubs={"numpy": None},
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert records == []  # explicit opt-out: nothing warmed
    assert _assert_probe_provenance(out) == []


def test_worker_prewarm_missing_module_is_nonfatal(tmp_path):
    """A configured module absent from the environment is skipped silently (ImportError):
    the worker must still boot and warm the remaining entries."""
    from agent.managed_worker import PREWARM_MODULES_ENV

    out, records = _prewarm_list_probe(
        tmp_path,
        extra_env={PREWARM_MODULES_ENV: "no_such_module_xyz_1893, alpha"},
        stubs={"alpha": None},
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert [r["name"] for r in records] == ["alpha"]
    assert "no_such_module_xyz_1893" not in out.stderr  # silent: optional dependency


def test_worker_prewarm_failing_module_warns_not_fatal(tmp_path):
    """A module that RAISES on import (broken install) logs a warning and the worker
    continues — the next entry still warms (split error policy, fcdd4d54f30)."""
    from agent.managed_worker import PREWARM_MODULES_ENV

    out, records = _prewarm_list_probe(
        tmp_path,
        extra_env={PREWARM_MODULES_ENV: "boom, alpha"},
        stubs={"alpha": None, "boom": "raise RuntimeError('prewarm-boom-sentinel')\n"},
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert [r["name"] for r in records] == ["alpha"]  # list keeps going past the failure
    assert "boom prewarm failed; worker continues without it" in out.stderr


def test_prewarm_module_names_parsing():
    from agent.managed_worker import _prewarm_module_names

    assert _prewarm_module_names(None) == ["numpy"]  # default
    assert _prewarm_module_names("") == []  # explicit opt-out
    assert _prewarm_module_names("numpy") == ["numpy"]
    assert _prewarm_module_names(" a, b,, a  c ") == ["a", "b", "c"]  # dedupe, both separators
    assert _prewarm_module_names("scipy.sparse") == ["scipy.sparse"]  # dotted names pass through
