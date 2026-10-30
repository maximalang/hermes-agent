"""`kanban create/show --json` must expose `idempotency_key` (#t_1dbabaac).

Idempotent creators (aux_failure_watcher K4) verify their write by reading
the card back: create `--idempotency-key K --json` prints the bare task
dict, `show <id> --json` prints the wrapped ``{"task": {...}}`` envelope.
Both must carry `idempotency_key`, or a safe server-side dedupe is
indistinguishable from a lost write and pending state never clears.
"""

from __future__ import annotations

import argparse
import json

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(__import__("pathlib").Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _run(cmd, *argv):
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    args = root.parse_args(["kanban", cmd, *argv])
    handler = {"create": kc._cmd_create, "show": kc._cmd_show}[cmd]
    return handler(args)


def test_create_json_prints_bare_task_dict_with_idempotency_key(kanban_home, capsys):
    assert _run("create", "PROBE", "--assignee", "baxter", "--idempotency-key", "K1", "--json") == 0
    created = json.loads(capsys.readouterr().out)
    # create --json is the bare task dict (no envelope): idempotent creators
    # read the id straight off the top level.
    assert created["idempotency_key"] == "K1"
    assert created["id"]


def test_show_json_task_envelope_carries_idempotency_key(kanban_home, capsys):
    assert _run("create", "PROBE", "--assignee", "baxter", "--idempotency-key", "K1", "--json") == 0
    tid = json.loads(capsys.readouterr().out)["id"]
    capsys.readouterr()
    assert _run("show", tid, "--json") == 0
    shown = json.loads(capsys.readouterr().out)
    # show --json wraps the task: the readback contract is saved["task"]["id"]
    # and saved["task"]["idempotency_key"].
    assert shown["task"]["id"] == tid
    assert shown["task"]["idempotency_key"] == "K1"


def test_duplicate_key_returns_existing_card(kanban_home, capsys):
    assert _run("create", "PROBE", "--assignee", "baxter", "--idempotency-key", "K1", "--json") == 0
    first = json.loads(capsys.readouterr().out)
    capsys.readouterr()
    assert _run("create", "PROBE", "--assignee", "baxter", "--idempotency-key", "K1", "--json") == 0
    second = json.loads(capsys.readouterr().out)
    assert second["id"] == first["id"]
    assert second["idempotency_key"] == "K1"


def test_task_without_key_serializes_null_not_missing(kanban_home, capsys):
    assert _run("create", "PROBE", "--assignee", "baxter", "--json") == 0
    created = json.loads(capsys.readouterr().out)
    # A stable schema beats a sometimes-present key: consumers must be able to
    # distinguish "no key" (None) from "key unknown" (absent).
    assert "idempotency_key" in created
    assert created["idempotency_key"] is None
