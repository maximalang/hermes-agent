"""Persist-only webhook ingress: real HTTP, no gateway daemon or live home."""

import hashlib
import hmac
import json
from unittest.mock import patch

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter
from gateway.platforms import webhook_cron_events as inbox

KEY = "test-only-key"


def signed(raw, delivery="delivery-1", key=KEY):
    return {"X-Hub-Signature-256": "sha256=" + hmac.new(key.encode(), raw, hashlib.sha256).hexdigest(),
            "X-GitHub-Delivery": delivery, "X-GitHub-Event": "check_run"}


@pytest_asyncio.fixture
async def receiver(tmp_path):
    with patch.object(inbox, "get_scoped_secret", return_value=KEY):
        instance = inbox.CronEventReceiver.from_config(
            {"cron_events_root": str(tmp_path), "cron_events_sources": ["github", "other"]}, 1024)
    app = web.Application(client_max_size=1024)
    app.router.add_post("/cron-events/{source}", instance.handle)
    async with TestClient(TestServer(app)) as client:
        yield client, tmp_path, instance


@pytest.mark.asyncio
async def test_auth_persist_replay_and_restart(receiver):
    client, root, _instance = receiver
    raw = b'{"action":"completed"}'
    assert (await client.post("/cron-events/github", data=raw)).status == 401
    assert not list(root.rglob("*.json"))
    assert (await client.post("/cron-events/github", data=raw, headers=signed(raw))).status == 204
    files = list((root / "inbox/github").glob("*.json"))
    assert len(files) == 1
    envelope = json.loads(files[0].read_text())
    assert envelope["payload"] == {"action": "completed"}
    assert envelope["delivery_id"] == "delivery-1"
    assert "X-Hub-Signature-256" not in envelope["headers"]
    assert (await client.post("/cron-events/github", data=raw, headers=signed(raw))).status == 204
    # Reconstructing the receiver must not lose the persisted dedupe ledger.
    with patch.object(inbox, "get_scoped_secret", return_value=KEY):
        restarted = inbox.CronEventReceiver(root, ["github"], 1024)
    assert restarted._persist("github", signed(raw), raw).status == 204
    assert len(list((root / "inbox/github").glob("*.json"))) == 1
    # A delivery ID on another authenticated source is not a replay.
    assert (await client.post("/cron-events/other", data=raw, headers=signed(raw))).status == 204
    assert len(list((root / "inbox/other").glob("*.json"))) == 1


@pytest.mark.asyncio
async def test_fail_closed_and_body_bounds(receiver):
    client, root, instance = receiver
    raw = b"{}"
    assert (await client.post("/cron-events/nope", data=raw, headers=signed(raw))).status == 404
    assert (await client.post("/cron-events/github", data=raw, headers=signed(raw, key="wrong"))).status == 401
    instance.keys["github"] = ""
    assert (await client.post("/cron-events/github", data=raw, headers=signed(raw))).status == 503
    instance.keys["github"] = KEY
    large = b"x" * 2048
    assert (await client.post("/cron-events/github", data=large, headers=signed(large))).status == 413
    async def chunks():
        yield large
    assert (await client.post("/cron-events/github", data=chunks(), headers=signed(large))).status == 413
    assert not list(root.rglob("*.json"))


@pytest.mark.asyncio
async def test_write_failure_retries_and_ledger_failure_does_not_lose_event(receiver):
    client, root, _instance = receiver
    raw = b"{}"
    with patch.object(inbox, "_publish", side_effect=OSError("injected")):
        assert (await client.post("/cron-events/github", data=raw, headers=signed(raw))).status == 500
    assert not list(root.rglob("*.json"))
    publish = inbox._publish
    def failed_ledger(path, value):
        if path.parent.name == "state":
            raise OSError("injected ledger error")
        publish(path, value)
    with patch.object(inbox, "_publish", side_effect=failed_ledger):
        assert (await client.post("/cron-events/github", data=raw, headers=signed(raw))).status == 204
    assert len(list((root / "inbox/github").glob("*.json"))) == 1


@pytest.mark.asyncio
async def test_non_json_and_corrupt_ledger_are_recoverable(receiver):
    client, root, _instance = receiver
    path = root / "state/seen-deliveries-github.json"
    path.parent.mkdir(parents=True)
    path.write_text("broken")
    raw = b"\xffinvalid-json"
    assert (await client.post("/cron-events/github", data=raw, headers=signed(raw))).status == 204
    envelope = json.loads(next((root / "inbox/github").glob("*.json")).read_text())
    assert envelope["payload"] == {"_raw_b64": "/2ludmFsaWQtanNvbg=="}


@pytest.mark.asyncio
async def test_real_adapter_connect_mounts_opt_in_route_without_agent(tmp_path):
    with patch.object(inbox, "get_scoped_secret", return_value=KEY):
        adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={
            "host": "127.0.0.1", "port": 0, "cron_events_root": str(tmp_path)}))
    assert await adapter.connect()
    try:
        port = next(iter(adapter._runner.sites))._server.sockets[0].getsockname()[1]
        async with aiohttp.ClientSession() as client:
            base = f"http://127.0.0.1:{port}"
            raw = b"{}"
            assert (await client.get(base + "/health")).status == 200
            assert (await client.post(base + "/cron-events/github", data=raw)).status == 401
            assert (await client.post(base + "/cron-events/github", data=raw, headers=signed(raw))).status == 204
            assert (await client.post(base + "/webhooks/missing", data=raw)).status == 404
        assert len(list((tmp_path / "inbox/github").glob("*.json"))) == 1
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_disabled_by_default():
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"host": "127.0.0.1", "port": 0}))
    assert await adapter.connect()
    try:
        port = next(iter(adapter._runner.sites))._server.sockets[0].getsockname()[1]
        async with aiohttp.ClientSession() as client:
            assert (await client.post(f"http://127.0.0.1:{port}/cron-events/github", data=b"{}")).status == 404
    finally:
        await adapter.disconnect()


@pytest.mark.parametrize("extra", [
    {"cron_events_root": "relative"},
    {"cron_events_root": 42},
    {"cron_events_sources": ["../escape"]},
    {"cron_events_sources": "github"},
])
def test_invalid_config_refused(extra, tmp_path):
    extra.setdefault("cron_events_root", str(tmp_path))
    with pytest.raises(ValueError):
        inbox.CronEventReceiver.from_config(extra, 1024)


def test_override_keys_captured_in_own_scope(tmp_path):
    keys = {"CRON_EVENTS_HMAC_SECRET": KEY, "CRON_EVENTS_SECRET__OTHER": "other-key"}
    with patch.object(inbox, "get_scoped_secret", side_effect=lambda name: keys.get(name)):
        receiver = inbox.CronEventReceiver(tmp_path, ["github", "other"], 1024)
    assert receiver.keys == {"github": KEY, "other": "other-key"}
    # A later request's ambient scope cannot change the receiver's authority.
    keys.clear()
    assert receiver.keys["github"] == KEY


def test_bom_ledger_preserves_dedupe(tmp_path):
    path = tmp_path / "seen.json"
    path.write_text('\ufeff{"ids": ["known"]}', encoding="utf-8")
    assert inbox._load_seen(path) == ["known"]


def test_failed_atomic_publication_leaves_no_consumer_file(tmp_path):
    path = tmp_path / "inbox" / "event.json"
    with patch.object(inbox.os, "replace", side_effect=OSError("injected rename error")):
        with pytest.raises(OSError):
            inbox._publish(path, {"schema": 1})
    assert not list(path.parent.iterdir())
