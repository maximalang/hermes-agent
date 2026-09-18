"""E2E canary for the rebased autocompany branch.

Runs the FULL lifecycle on a throwaway HERMES_HOME through the real CLI
(same entrypoint the dispatcher uses), asserting the P0/P1/P2 contract:

1. create -> claim -> work -> complete: a durable terminal receipt is
   written automatically (worker never touches the receipt schema).
2. request_review -> independent reviewer -> done (review lane).
3. `kanban metrics` reports receipt coverage from the run ledger.
4. Exit codes and statuses are read back from the DB, not from stdout prose.

Usage: python e2e_canary_rebased.py
Exit 0 = PASS, 1 = FAIL.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

REPO = r"C:/Users/max/AppData/Local/Temp/autocompany-p0"
PY = r"C:/Users/max/AppData/Local/hermes/hermes-agent/.venv/Scripts/python.exe"
HOME = tempfile.mkdtemp(prefix="e2e-canary-")
FAILURES = []


def _json_out(text: str):
    """Parse the last complete JSON object/array from CLI --json output."""
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        if start >= 0:
            try:
                return json.loads(text[start:])
            except Exception:
                pass
            # fall back: try progressively shorter tails
            end = text.rfind(closer)
            while end > start:
                try:
                    return json.loads(text[start:end + 1])
                except Exception:
                    end = text.rfind(closer, start, end)
    return None



def cli(*args, env_extra=None):
    env = dict(os.environ)
    env.update({
        "HERMES_HOME": HOME,
        "PYTHONPATH": REPO,
        "PYTHONIOENCODING": "utf-8",
    })
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [PY, "-m", "hermes_cli.main", "kanban", *args],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=120,
    )
    return proc


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


def main():
    t0 = time.time()
    env_profile = {"HERMES_PROFILE": "company"}

    # 1. create
    r = cli("create", "canary W: implement widget", "--assignee", "tech",
            "--json", env_extra=env_profile)
    check("create exit 0", r.returncode == 0, f"rc={r.returncode} {r.stderr[-200:]}")
    created = _json_out(r.stdout)
    if created is None:
        print(r.stdout[-400:])
        check("create returns json", False)
        return 1
    w_id = created.get("id") or created.get("task", {}).get("id")
    check("create returns task id", bool(w_id), str(w_id))

    # 2. claim (worker leg)
    r = cli("claim", w_id, env_extra=env_profile)
    check("claim exit 0", r.returncode == 0, r.stderr[-150:])

    # 3. complete with evidence -> durable auto receipt
    r = cli("complete", w_id, "--summary", "canary evidence: widget built",
            env_extra=env_profile)
    check("complete exit 0", r.returncode == 0, r.stderr[-200:])

    # 4. read back status + receipt from the DB (not stdout prose)
    r = cli("show", w_id, "--json", env_extra=env_profile)
    check("show exit 0", r.returncode == 0)
    shown = _json_out(r.stdout)
    if shown is None:
        check("show returns json", False, r.stdout[-200:])
    else:
        task = shown.get("task", shown)
        status = task.get("status")
        check("task status done (no goal_mode: direct close)", status == "done", str(status))

    # receipt readback straight from the sqlite run ledger:
    # _end_run attaches metadata['receipt'] atomically in the same txn
    import sqlite3
    db = None
    for root, _dirs, files in os.walk(HOME):
        for f in files:
            if f.endswith(".db"):
                cand = os.path.join(root, f)
                try:
                    conn = sqlite3.connect(f"file:{cand}?mode=ro", uri=True)
                    conn.execute("SELECT name FROM sqlite_master WHERE name='task_runs'").fetchone()
                    db = conn
                    break
                except Exception:
                    pass
        if db:
            break
    receipts = []
    if db:
        try:
            rows = db.execute(
                "SELECT task_id, outcome, metadata FROM task_runs ORDER BY id"
            ).fetchall()
            for tid, outcome, meta in rows:
                try:
                    md = json.loads(meta or "{}")
                except Exception:
                    md = {}
                rec = md.get("receipt")
                receipts.append({"task_id": tid, "outcome": outcome, "receipt": rec})
        except Exception as exc:
            check("run ledger readable", False, str(exc))
    else:
        check("kanban db found", False, HOME)
    w_receipts = [r for r in receipts if r["task_id"] == w_id and r["receipt"]]
    check("durable auto receipt written", bool(w_receipts), json.dumps(receipts)[:300])
    if w_receipts:
        check("receipt status == done", w_receipts[-1]["receipt"].get("status") == "done",
              json.dumps(w_receipts[-1]["receipt"]))


    # 5. review lane: request_review -> reviewer approve -> done
    r = cli("create", "canary R: review widget", "--assignee", "qa", "--json",
            env_extra=env_profile)
    created_r = _json_out(r.stdout)
    rid = created_r.get("id") if isinstance(created_r, dict) else None
    check("review-lane task created", bool(rid), str(rid))
    if rid:
        r = cli("claim", rid, env_extra=env_profile)
        check("review-lane claim", r.returncode == 0, r.stderr[-120:])
        r = cli("request-review", rid, "--summary", "canary: implementation ready",
                env_extra=env_profile)
        check("request-review exit 0", r.returncode == 0, r.stderr[-150:])
        # reviewer side
        r = cli("complete", rid, "--summary", "reviewer verdict: approved",
                env_extra={"HERMES_PROFILE": "qa"})
        check("reviewer complete exit 0", r.returncode == 0, r.stderr[-150:])
        r = cli("show", rid, "--json", env_extra=env_profile)
        stj = _json_out(r.stdout)
        st = (stj.get("task", stj) if isinstance(stj, dict) else {}).get("status") or "?"
        check("review-lane final status done", st == "done", str(st))

    # 6. metrics surface
    r = cli("metrics", "--json", env_extra=env_profile)
    check("metrics exit 0", r.returncode == 0, r.stderr[-150:])
    m = _json_out(r.stdout)
    if m is None:
        check("metrics returns json", False, r.stdout[-200:])
    else:
        cov = m.get("receipt_coverage_pct")
        check("metrics receipt_coverage_pct is a number", isinstance(cov, (int, float)), str(cov))
        check("metrics completed>=2", int(m.get("completed_tasks") or 0) >= 2,
              str(m.get("completed_tasks")))

    dt = time.time() - t0
    print(f"\n{'=' * 46}\nRESULT: {'FAIL' if FAILURES else 'PASS'} "
          f"({len(FAILURES)} failures) in {dt:.1f}s\n{'=' * 46}")
    for f in FAILURES:
        print("  failed check:", f)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
