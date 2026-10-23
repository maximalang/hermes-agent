"""Deterministic Windows loader-lock ABBA repro (t_dc7c5148 / t_d4350193 follow-up).

WHAT THIS REPRODUCES
--------------------
The 2026-10-21 kanban worker wedge class: a C-extension DLL whose DllMain waits on
thread synchronization while holding the OS loader lock (libscipy_openblas64 builds
its thread pool from DllMain), racing a second thread that already holds that
synchronization and now needs the loader lock for its own DLL load. Native py-spy
stacks of the wedged worker (run 5417): DllMain inside LoadLibrary on the main
thread, reader threads inside pthread/CS code waiting for the loader lock, CPU 0%.

The stock standalone probes (`import hermes_bootstrap, numpy` in a fresh child, same
scrubbed env) complete in ~0.1 s and never reproduce the wedge: OpenBLAS's pool-wait
is timing-dependent (pool thread creation must lose a race that a fresh process
usually wins). This script removes the race: a purpose-built DLL *guarantees* the
DllMain-wait, so the circular wait is structural, not probabilistic.

    Lock L1 = Windows loader lock (held by the main thread for the whole DllMain)
    Lock L2 = "go" event — T sets it only AFTER its own LoadLibrary returns

    main: LoadLibrary(dll)      -> holds L1, DllMain waits for L2
    T   : LoadLibrary(dll)      -> wants L1 (blocked), then would set L2
    => T waits for L1 (main), main waits for L2 (T): ABBA, CPU 0%, forever.

Two stall points observed in the wedge child (both match the production py-spy
signature, run 5417):
  * T never reaches its first print: a Python thread start runs DLL_THREAD_ATTACH
    for every loaded DLL, which needs the loader lock, so the wedge catches T in
    bootstrap — the "6 wedged threads" signature of the real worker.
  * os._exit cannot kill the process: _exit reaches ExitProcess, which walks
    DLL_PROCESS_DETACH under the loader lock and blocks. That is why the wedged
    workers became unkillable zombies (PID alive, CPU 0%, no reaper collected
    them); the child watchdog therefore kills with TerminateProcess.

RUN
---
    python scripts/repro_loaderlock_abba.py

Requires Windows + gcc (mingw-w64) on PATH to build the two tiny DLLs into a temp
dir at run time (nothing is installed). Runs two scenarios in separate children:

    control: DllMain returns immediately       -> child prints CHILD-OK, exit 0
    wedge  : DllMain waits for the "go" event  -> child watchdog os._exit(2)

Exit codes (parent):
    0  control completed AND wedge wedged (both verdicts as predicted)
    1  any verdict mismatch (harness bug — investigate, do not ship around it)
    2  compiler or platform prerequisite missing (see message; on hosts without
       gcc the documented boundary applies instead of this artifact)
"""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

if sys.platform != "win32":
    print(f"loader-lock ABBA repro: Windows-only deadlock class (sys.platform={sys.platform!r})")
    sys.exit(2)

WEDGE_SECONDS = 6          # child watchdog: DllMain/LoadLibrary unfinished after this = wedge
PARENT_TIMEOUT = 40        # hard cap for one child, well above watchdog + compile slack

C_SOURCE = r"""
#include <windows.h>

BOOL WINAPI DllMain(HINSTANCE inst, DWORD reason, LPVOID reserved) {
    (void)inst; (void)reserved;
    if (reason != DLL_PROCESS_ATTACH) return TRUE;
    HANDLE entered = CreateEventA(NULL, FALSE, FALSE, ENTERED_NAME);
    if (!entered) return FALSE;
#ifdef NO_WAIT
    SetEvent(entered);              /* control build: leave DllMain immediately */
    CloseHandle(entered);
    return TRUE;
#else
    /* Wedge build: we hold the loader lock for the whole DllMain. OpenBLAS waits
       for its pool threads from exactly this position; we wait for the go event. */
    HANDLE go = CreateEventA(NULL, FALSE, FALSE, GO_NAME);
    if (!go) { CloseHandle(entered); return FALSE; }
    SetEvent(entered);              /* "DllMain reached" -- main thread holds L1 now */
    DWORD r = WaitForSingleObject(go, INFINITE);
    CloseHandle(go); CloseHandle(entered);
    return r == WAIT_OBJECT_0;
#endif
}
"""


def _c_literal(name: str) -> str:
    return '"' + name.replace("\\", "\\\\") + '"'


def build_dlls(workdir: str, entered: str, go: str):
    """Compile the control and wedge DLLs; returns (control_path, wedge_path)."""
    gcc = shutil.which("gcc")
    if not gcc:
        print("loader-lock ABBA repro: gcc not found on PATH — cannot build the probe DLLs.")
        print("Install mingw-w64 (winget install BrechtSanders.WinLibs.POSIX.UCRT.LLVM) or use")
        print("the documented-boundary note instead of this artifact.")
        sys.exit(2)
    src = os.path.join(workdir, "abba_dll.c")
    with open(src, "w", encoding="ascii") as fh:
        fh.write(C_SOURCE)
    out = []
    for suffix, extra in (("control", ["-DNO_WAIT"]), ("wedge", [])):
        dll = os.path.join(workdir, f"abba_{suffix}.dll")
        cmd = [gcc, "-shared", "-Wall", *extra,
               "-DENTERED_NAME=" + _c_literal(entered), "-DGO_NAME=" + _c_literal(go),
               "-o", dll, src]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
        if proc.returncode != 0 or not os.path.exists(dll):
            print(f"gcc failed ({suffix}):\n{proc.stdout}\n{proc.stderr}")
            sys.exit(1)
        out.append(dll)
    return out[0], out[1]


def run_child(dll: str, entered: str, go: str) -> subprocess.CompletedProcess:
    """One scenario child; re-exec this file so the artifact stays a single file."""
    cmd = [sys.executable, os.path.abspath(__file__), "--child", dll, entered, go]
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=PARENT_TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        # A child so wedged its own watchdog could not fire: still a wedge verdict.
        return subprocess.CompletedProcess(cmd, returncode=2,
                                           stdout="", stderr="parent timeout (harder wedge)")


def child_main(dll: str, entered_name: str, go_name: str) -> None:
    """In-child scenario. Prints a timeline; watchdog os._exit(2) on wedge."""
    from ctypes import windll    # stdlib only; no hermes imports in the child

    k32 = windll.kernel32
    entered = k32.CreateEventA(None, False, False, entered_name.encode("ascii"))
    go = k32.CreateEventA(None, False, False, go_name.encode("ascii"))
    if not entered or not go:
        print("child: CreateEvent failed"); sys.exit(1)

    def _wedge_report_and_die():
        # TerminateProcess, NOT os._exit: _exit reaches ExitProcess, which walks
        # DLL_PROCESS_DETACH under the loader lock and therefore BLOCKS on this
        # very wedge — the same reason the real wedged workers turned into
        # zombies no reaper could collect (PID alive, CPU 0%, forever).
        print("child: WEDGE (watchdog fired): main never left DllMain, T never"
              " finished LoadLibrary; killing via TerminateProcess (os._exit"
              " would itself wait for the loader lock)", flush=True)
        # ctypes int args are 32-bit; HANDLE is pointer-wide — without explicit
        # argtypes the -1 pseudo-handle reaches TerminateProcess mangled and the
        # call fails (learned the hard way: the wedge then outlives its watchdog).
        import ctypes as _ct
        k32.GetCurrentProcess.restype = _ct.c_void_p
        k32.TerminateProcess.argtypes = [_ct.c_void_p, _ct.c_uint]
        k32.TerminateProcess(k32.GetCurrentProcess(), 2)
        os._exit(2)  # fallback, unreachable

    watchdog = threading.Timer(WEDGE_SECONDS, _wedge_report_and_die)
    watchdog.daemon = True
    watchdog.start()

    def t_body():
        # T = the wedged worker's reader thread: holds "its own" sync (here: nothing
        # else), then needs the loader lock for a DLL load. It signals go only after
        # that load RETURNS — which is what makes the wait graph circular.
        k32.WaitForSingleObject(entered, 0xFFFFFFFF)
        print("T:   loader-lock attempt (LoadLibrary) — blocking call follows", flush=True)
        k32.LoadLibraryW(dll)
        k32.SetEvent(go)
        print("T:   LoadLibrary returned, go set", flush=True)

    t = threading.Thread(target=t_body, name="reader-thread-standin", daemon=True)
    t.start()
    print("main: LoadLibrary -> DllMain (loader lock held for all of DllMain)", flush=True)
    h = k32.LoadLibraryW(dll)
    if not h:
        print("child: LoadLibrary failed"); os._exit(1)
    # Control path: DllMain returned; T's load also completes; both go events fire.
    k32.SetEvent(go)
    t.join(timeout=WEDGE_SECONDS)
    if t.is_alive():
        print("child: T stuck after main finished?!"); os._exit(1)
    watchdog.cancel()
    print("CHILD-OK", flush=True)
    os._exit(0)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        child_main(sys.argv[2], sys.argv[3], sys.argv[4])
        return  # unreachable (child exits)

    run = str(int(time.time() * 1000) % 10**9)
    entered = f"Local\\llabba_{run}_entered"
    go = f"Local\\llabba_{run}_go"
    workdir = tempfile.mkdtemp(prefix="llabba-")
    control_dll, wedge_dll = build_dlls(workdir, entered, go)

    verdicts = {}
    for name, dll in (("control", control_dll), ("wedge", wedge_dll)):
        proc = run_child(dll, entered, go)
        tail = (proc.stdout or "").strip().splitlines()
        last = tail[-1] if tail else ""
        wedged = proc.returncode == 2 or "WEDGE" in (proc.stdout or "")
        ok = proc.returncode == 0 and "CHILD-OK" in (proc.stdout or "")
        verdicts[name] = ok if name == "control" else wedged
        print(f"[{name}] rc={proc.returncode} last={last[:110]!r}")
        if proc.stderr:
            print(f"[{name}] stderr tail: {proc.stderr.strip()[-300:]}")

    if verdicts.get("control") and verdicts.get("wedge"):
        print("VERDICT: control completed AND wedge deadlocked — loader-lock ABBA"
              f" reproduced deterministically (DLLs: {workdir})")
        sys.exit(0)
    print(f"VERDICT: mismatch {verdicts!r} — harness bug, investigate")
    sys.exit(1)


if __name__ == "__main__":
    main()
