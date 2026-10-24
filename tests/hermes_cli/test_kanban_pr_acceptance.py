"""Two lifecycle invariants, using real SQLite and a local GitHub HTTP contract."""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_pr_acceptance as kpa
from hermes_cli.kanban_db_connect import connect


@pytest.fixture
def github(tmp_path, monkeypatch):
    state = {"conclusion": "success", "head": "a" * 40, "reads": 0, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append(self.path)
            sha = state["head"]
            if self.path == "/graphql":
                protection = None if state.get("no_protection") else {
                    "branchProtectionRule": {"requiredStatusChecks": [
                        {"context": "required", "app": {"databaseId": 1}}]}}
                value = {"data": {"repository": {"pullRequest": {
                    "headRefOid": sha, "baseRefName": "main",
                    "state": "MERGED" if state.get("pr_merged") else "OPEN",
                    "baseRef": protection}}}}
            elif "/rules/branches/" in self.path:
                if state.get("rules_refusal"):
                    # A real gh renders this as `gh: <message> (HTTP 403)` on stderr.
                    self.send_response(403)
                    self.end_headers()
                    self.wfile.write(json.dumps({"message": state["rules_refusal"]}).encode())
                    return
                value = [[]]
            elif "/check-runs" in self.path:
                run = {"id": 42, "name": "required", "head_sha": sha,
                       "app": {"id": 1}, "status": "in_progress" if state["conclusion"] == "pending" else "completed", "conclusion": state["conclusion"],
                       "html_url": "https://github.com/acme/repo/actions/runs/42"}
                if state.get("stale"):
                    run["head_sha"] = "b" * 40
                runs = [] if state.get("missing") else [run]
                value = [{"total_count": 100 + len(runs), "check_runs": [
                    {**run, "id": 1000 + i, "name": "optional", "conclusion": "skipped"}
                    for i in range(100)]}, {"total_count": 100 + len(runs), "check_runs": runs}]
                if state.get("race"):
                    state["race"]()
                if state.get("head_change"):
                    state["head"] = "b" * 40
            elif "/statuses" in self.path:
                value = [[]]
            elif "/pulls/" in self.path:
                merged = bool(state.get("pr_merged"))
                value = {"head": {"sha": sha}, "base": {"ref": "main"},
                         "state": "closed" if merged else "open", "merged": merged,
                         "merge_commit_sha": "c" * 40 if merged else None}
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    gh.write_text(f"#!{sys.executable}\n"
                  "import json, sys, urllib.error, urllib.request\n"
                  f"base='http://127.0.0.1:{server.server_port}/'\n"
                  "try:\n"
                  "    if sys.argv[1] == 'pr':  # gh pr view N --repo [HOST/]OWNER/REPO --json state,mergeCommit\n"
                  "        n = next(a for a in sys.argv[2:] if a.isdigit())\n"
                  "        repo = sys.argv[sys.argv.index('--repo') + 1].split('/', 1)[-1]\n"
                  "        pull = json.loads(urllib.request.urlopen(base + 'repos/' + repo + '/pulls/' + n).read())\n"
                  "        merged = pull.get('merged')\n"
                  "        print(json.dumps({'state': 'MERGED' if merged else pull['state'].upper(),\n"
                  "                          'mergeCommit': {'oid': pull['merge_commit_sha']} if merged else None}))\n"
                  "    else:  # gh api ENDPOINT --hostname github.com [...]\n"
                  "        print(urllib.request.urlopen(base + sys.argv[2]).read().decode())\n"
                  "except urllib.error.HTTPError as err:  # gh renders failures as `gh: <msg> (HTTP <code>)`\n"
                  "    print('gh: ' + json.loads(err.read().decode()).get('message') + ' (HTTP ' + str(err.code) + ')',\n"
                  "          file=sys.stderr)\n"
                  "    sys.exit(1)\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    kb.init_db()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.platforms("linux")
def test_pr_completion_requires_current_required_evidence(github):
    with connect() as conn:
        for conclusion in ("failure", "pending", "cancelled", "timed_out", "action_required", "neutral", "skipped", None, "success"):
            github.update(conclusion=conclusion, head="a" * 40)
            tid = kb.create_task(conn, title="Publish", completion_contract="acme/repo")
            ok = kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert ok is (conclusion == "success")
            task = kb.get_task(conn, tid)
            assert (task.status == "done") is ok
            receipts = [json.loads(r[0]) for r in conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,))]
            assert receipts and receipts[-1]["head_sha"] == "a" * 40
            if not ok:
                assert task.status in {"running", "ready", "blocked", "review"}
                assert "retry" in receipts[-1]["recovery"]
                assert receipts[-1]["checks"][0]["id"] == 42
        for fault in ("missing", "stale", "head_change"):
            github.update(conclusion="success", head="a" * 40)
            github[fault] = True
            tid = kb.create_task(conn, title=fault, completion_contract="acme/repo")
            assert not kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).status != "done"
            github.pop(fault)
        # Omission and a sibling repository cannot downgrade the stored declaration.
        tid = kb.create_task(conn, title="publish", completion_contract="acme/repo")
        assert not kb.complete_task(conn, tid, summary="local green")
        assert not kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/other/repo/pull/7"})
        before = len(github["requests"])
        local = kb.create_task(conn, title="local", completion_contract="local-only")
        assert kb.complete_task(conn, local, summary="https://github.com/acme/repo/pull/7 is background context")
        assert len(github["requests"]) == before


@pytest.mark.platforms("linux")
def test_acceptance_receipts_and_terminal_write_share_run_ownership(github):
    with connect() as conn:
        for conclusion in ("success", "failure"):
            tid = kb.create_task(conn, title="race", completion_contract="acme/repo")
            owner = kb.claim_task(conn, tid)
            run_id = owner.current_run_id
            def reclaim():
                with connect() as rival:
                    assert kb.block_task(rival, tid, reason="Reassigned during acceptance")
                    assert kb.unblock_task(rival, tid)
                    github["replacement"] = kb.claim_task(rival, tid).current_run_id
            github.update(conclusion=conclusion, race=reclaim)
            assert not kb.complete_task(conn, tid, result="done", expected_run_id=run_id,
                metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).current_run_id == github["replacement"]
            assert github["replacement"] != run_id
            assert kb.get_task(conn, tid).status != "done"
            assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0] == 0
            github.pop("race")


# --- #122689: acceptance must read the repo as the ASSIGNEE profile's gh login ---

@pytest.mark.platforms("posix")
def test_acceptance_runs_gh_as_the_assignee_profile(tmp_path, monkeypatch):
    """The gh child env carries the assignee's own GH credentials (its .env),
    never the ambient/launch residue, and an invisible repo is classified
    `auth` naming the repository — not a retryable `infra` failure."""
    from pathlib import Path

    launch_home = tmp_path / "home"
    launch_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    assignee_home = launch_home / "profiles" / "b"
    assignee_home.mkdir(parents=True)
    (assignee_home / ".env").write_text("GH_TOKEN=b-token\n", encoding="utf-8")
    # Ambient residue that must NOT decide the login.
    monkeypatch.setenv("GH_TOKEN", "launch-token")
    monkeypatch.setenv("GH_CONFIG_DIR", "/nonexistent/launch/gh")

    env_dump = tmp_path / "gh_env.json"
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    gh.write_text(f"#!{sys.executable}\nimport json, os\n"
                  f"json.dump(dict(os.environ), open({str(env_dump)!r}, 'w'))\n"
                  "print(json.dumps({'data': {'repository': None}}))\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="as-b", completion_contract="acme/repo", assignee="b")
        assert not kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        assert kb.get_task(conn, tid).status != "done"
        receipts = [json.loads(r[0]) for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,))]
        assert receipts[-1]["classification"] == "auth"
        assert "acme/repo" in receipts[-1]["detail"]
    captured = json.loads(env_dump.read_text())
    assert captured["GH_TOKEN"] == "b-token"
    assert captured.get("GH_CONFIG_DIR") != "/nonexistent/launch/gh"
    assert "credentials" in (kb.get_task(conn, tid).last_failure_error or "")


@pytest.mark.platforms("posix")
def test_assignee_without_own_gh_login_never_falls_through_to_ambient_login(tmp_path, monkeypatch):
    """An assignee profile with no GH_TOKEN/GH_CONFIG_DIR of its own must not inherit the
    launch user's ~/.config/gh (HOME/XDG_CONFIG_HOME stay the launch process's): gh is pinned
    to a profile-owned config dir, its 'not logged in' exit is classified `auth` naming the profile."""
    launch_home = tmp_path / "home"
    launch_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    assignee_home = launch_home / "profiles" / "b"
    assignee_home.mkdir(parents=True)
    (assignee_home / ".env").write_text("", encoding="utf-8")
    monkeypatch.setenv("GH_TOKEN", "launch-token")
    monkeypatch.setenv("GH_CONFIG_DIR", "/nonexistent/launch/gh")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "launch-xdg"))

    env_dump = tmp_path / "gh_env.json"
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    # Real gh: GH_CONFIG_DIR wins; a config dir without hosts.yml means "not logged in" (exit 4).
    gh.write_text(f"#!{sys.executable}\nimport json, os, pathlib, sys\n"
                  f"pathlib.Path({str(env_dump)!r}).write_text(json.dumps(dict(os.environ)), encoding='utf-8')\n"
                  "if 'GH_CONFIG_DIR' in os.environ and not os.path.exists(os.environ['GH_CONFIG_DIR']):\n"
                  "    sys.exit(4)\n"
                  "print(json.dumps({'data': {'repository': None}}))\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="as-b", completion_contract="acme/repo", assignee="b")
        assert not kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0])
    assert receipt["classification"] == "auth"
    assert "'b'" in receipt["detail"] and "no login" in receipt["detail"]
    captured = json.loads(env_dump.read_text(encoding="utf-8-sig"))
    assert captured["GH_CONFIG_DIR"] == str(assignee_home / "gh")
    assert "GH_TOKEN" not in captured and "GITHUB_TOKEN" not in captured


def test_assigned_card_with_unresolvable_profile_is_auth_not_ambient(tmp_path, monkeypatch):
    """A card assigned to a profile that no longer exists must not run gh as the completing
    process's ambient login: classification `auth` naming the profile, gh never invoked."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))  # any gh spawn would fail as infra
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="as-ghost", completion_contract="acme/repo", assignee="ghost")
        assert not kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0])
    assert receipt["classification"] == "auth"
    assert "'ghost'" in receipt["detail"] and "cannot be resolved" in receipt["detail"]


# --- feature-gated rules API: private repositories without GitHub Pro ------------------
#
# GET /repos/{owner}/{repo}/rules/branches/{branch} answers HTTP 403
# "Upgrade to GitHub Pro or make this repository public to enable this feature."
# there — a paywall, not a credentials problem: no branch rules CAN be configured.
# Observed live on maximalang/ventures (gh stderr: `gh: Upgrade to GitHub Pro or
# make this repository public to enable this feature. (HTTP 403)`).

_FEATURE_GATE_BODY = ("Upgrade to GitHub Pro or make this repository public "
                      "to enable this feature.")


def _gh_mock(monkeypatch, *, http_status=403, body=_FEATURE_GATE_BODY,
             pr_state="MERGED", merge_commit="c" * 40, calls=None):
    """Replace ``kanban_pr_acceptance.subprocess.run`` with a canned gh whose
    rules read fails exactly like the real one — ``gh: <body> (HTTP <status>)``
    on stderr, gh's exit 1 — and whose publication read answers
    ``gh pr view --json state,mergeCommit``."""
    def run(command, **kwargs):
        if calls is not None:
            calls.append(list(command))
        if command[1] == "pr":
            assert "--json" in command and "state,mergeCommit" in command
            stdout = json.dumps({"state": pr_state,
                                 "mergeCommit": {"oid": merge_commit} if merge_commit else None})
        elif command[2] == "graphql":
            stdout = json.dumps({"data": {"repository": {"pullRequest": {
                "headRefOid": "a" * 40, "baseRefName": "main", "state": pr_state,
                "baseRef": {"branchProtectionRule": None}}}}})
        elif "/rules/branches/" in command[2]:
            raise subprocess.CalledProcessError(
                1, command, stderr=f"gh: {body} (HTTP {http_status})")
        else:  # check-runs / statuses / pulls reads of the normal path
            stdout = "{}"
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")
    monkeypatch.setattr(kpa.subprocess, "run", run)


def test_feature_gate_403_rules_read_accepts_merged_publication(monkeypatch):
    """The paywall 403 means 'no rules are configured': with no required checks
    visible, acceptance falls back to publication — gh pr view must report
    state=MERGED with a mergeCommit, and the receipt accepts, not `auth`."""
    calls = []
    _gh_mock(monkeypatch, calls=calls)
    receipt = kpa.collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")
    assert receipt["ok"] is True
    assert receipt["classification"] == "success"
    assert receipt["head_sha"] == "a" * 40
    assert receipt["publication"] == {"state": "MERGED", "merge_commit": "c" * 40}
    assert any(command[1] == "pr" for command in calls)  # publication was actually verified


def test_403_without_feature_gate_marker_still_fails_closed_as_auth(monkeypatch):
    """A plain permissions 403 (no paywall marker) is a real auth failure:
    classification `auth`, no publication fallback is attempted."""
    calls = []
    _gh_mock(monkeypatch, body="Resource not accessible by integration", calls=calls)
    receipt = kpa.collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")
    assert receipt["ok"] is False
    assert receipt["classification"] == "auth"
    assert "HTTP 403" in receipt["detail"]
    assert "credentials" in receipt["detail"]
    assert not any(command[1] == "pr" for command in calls)


def test_feature_gate_marker_does_not_soften_a_401(monkeypatch):
    """Even a stderr carrying the paywall text cannot soften a 401: any 401
    stays an auth failure."""
    _gh_mock(monkeypatch, http_status=401)
    receipt = kpa.collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")
    assert receipt["ok"] is False
    assert receipt["classification"] == "auth"
    assert "HTTP 401" in receipt["detail"]


def test_feature_gate_403_with_open_pr_is_not_accepted(monkeypatch):
    """Feature-gated rules plus an unmerged PR is not a publication: fail
    closed with a retryable pending classification, never `auth`."""
    _gh_mock(monkeypatch, pr_state="OPEN", merge_commit=None)
    receipt = kpa.collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")
    assert receipt["ok"] is False
    assert receipt["classification"] == "pending"
    assert receipt["publication"]["state"] == "OPEN"


def test_feature_gate_403_merged_without_merge_commit_fails_closed(monkeypatch):
    """state=MERGED without a mergeCommit is not a publication either."""
    _gh_mock(monkeypatch, pr_state="MERGED", merge_commit=None)
    receipt = kpa.collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")
    assert receipt["ok"] is False
    assert receipt["classification"] == "missing"


@pytest.mark.platforms("linux")
def test_feature_gated_private_repo_lifecycle_accepts_merged_publication(github):
    """End to end on the HTTP contract: the rules endpoint answers the paywall
    403; a MERGED PR completes the card, while the same 403 without the marker
    still refuses completion as an auth failure."""
    github.update(no_protection=True, pr_merged=True, rules_refusal=_FEATURE_GATE_BODY)
    with connect() as conn:
        tid = kb.create_task(conn, title="feature-gate", completion_contract="acme/repo")
        assert kb.complete_task(conn, tid, result="done",
                                metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        assert kb.get_task(conn, tid).status == "done"
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0])
        assert receipt["ok"] is True and receipt["classification"] == "success"
        assert receipt["publication"] == {"state": "MERGED", "merge_commit": "c" * 40}

        github.update(rules_refusal="Resource not accessible by integration")
        tid = kb.create_task(conn, title="plain-403", completion_contract="acme/repo")
        assert not kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        assert kb.get_task(conn, tid).status != "done"
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0])
        assert receipt["classification"] == "auth"
        assert "credentials" in (kb.get_task(conn, tid).last_failure_error or "")
