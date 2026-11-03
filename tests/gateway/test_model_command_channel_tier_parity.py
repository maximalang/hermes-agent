"""TGR-02 / B1: /model channel-tier parity with the turn runtime.

Defect (B1, exposed live 05.10.2026): the ``/model`` text listing and the Telegram
picker returned the profile-global route (``gpt-6.1-sol`` on ``openai-codex``) for
the company Telegram DM ``1256122537`` and forum ``-1004426332349`` while the turn
runtime resolves the company ``channel_overrides`` tier (``qwen3.8-max`` on
``custom``) for those chats — the command advertised a route the next turn never
runs.

Parity fix (``_handle_model_command_locked``): ``_apply_channel_tier_to_context``
applies the routed channel tier between the global ``read_config`` and the session
pin's ``apply_override``, so the turn-runtime precedence is preserved — session
/model pin > channel tier > profile global — for the listing and the picker alike
(both read ``ctx.current_model`` / ``ctx.current_provider``).

All provider resolution is mocked; no network, no live config, no credentials.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import ChannelOverride, GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.session_identity import RoutingIdentity

# The profile-global route the defective /model display advertised ...
GLOBAL_MODEL = "gpt-6.1-sol"
GLOBAL_PROVIDER = "openai-codex"
# ... vs the channel tier the turn runtime actually resolves for company chats.
QWEN = "qwen3.8-max"
CUSTOM = "custom"
QWEN_URL = "https://qwen.test/v1"
COMPANY_DM = "1256122537"
COMPANY_FORUM = "-1004426332349"
PINNED_MODEL = "pinned/model"
PINNED_PROVIDER = "openai-codex"
PRIMARY_CHAT = "primary-desktop-chat"


def _identity(name: str) -> RoutingIdentity:
    return RoutingIdentity(
        transport_profile="default",
        runtime_profile=name,
        authorization_home=Path("."),
        runtime_home=Path("."),
        multiplexed=True,
    )


def _typed_config(overrides: dict | None = None) -> GatewayConfig:
    return GatewayConfig(
        multiplex_profiles=True,
        platforms={
            Platform.TELEGRAM: PlatformConfig(
                enabled=True, channel_overrides=dict(overrides or {}))
        },
    )


def _company_channel_overrides() -> dict:
    return {
        COMPANY_DM: ChannelOverride(model=QWEN, provider=CUSTOM),
        COMPANY_FORUM: ChannelOverride(model=QWEN, provider=CUSTOM),
    }


def _event(chat_id: str, *, thread_id: str | None = None, chat_type: str = "dm") -> MessageEvent:
    return MessageEvent(
        text="/model",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM, chat_id=chat_id, user_id="u1",
            chat_type=chat_type, thread_id=thread_id,
        ),
    )


def _pin_company(source: SessionSource) -> SessionSource:
    # Pin BOTH layers the router consults: the private identity attr (lost to
    # ``dataclasses.replace`` when ``_normalize_source_for_session_key`` rewrites a
    # source) and the ``profile`` field (which survives it).
    setattr(source, "_identity", _identity("company"))
    source.profile = "company"
    return source


class _CapturingPickerAdapter:
    def __init__(self):
        self.kwargs = None

    async def send_model_picker(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(success=True)


class _TextOnlyAdapter:
    """No ``send_model_picker`` attribute → ``/model`` falls back to the text listing."""


# Deliberately no model rows: in the text listing the QWEN name can then only come
# from the "Current:" line, so a plain containment assert anchors the parity.
_PROVIDER_ROWS = [{
    "slug": CUSTOM, "name": "Custom", "is_current": True,
    "models": [], "total_models": 0, "api_url": QWEN_URL,
}]


def _runner(tmp_path, monkeypatch, *, picker: bool, launch_overrides: dict | None = None,
            company_overrides: dict | None = None):
    """Bare multiplex runner serving 'company' with the company channel rules.

    The company home's config.yaml holds the profile-global route (Sol) that the
    defective display advertised; the launch home holds a different global so a
    primary chat can never pass by silently borrowing the company's.
    """
    import gateway.run as gateway_run

    default_home = tmp_path / "default"
    company_home = tmp_path / "profiles" / "company"
    default_home.mkdir(parents=True, exist_ok=True)
    company_home.mkdir(parents=True, exist_ok=True)
    (default_home / "config.yaml").write_text(
        "model:\n  default: default-model\n  provider: default-provider\n", encoding="utf-8")
    (company_home / "config.yaml").write_text(
        f"model:\n  default: {GLOBAL_MODEL}\n  provider: {GLOBAL_PROVIDER}\n", encoding="utf-8")

    monkeypatch.setattr(gateway_run, "_hermes_home", default_home)
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
    listing_calls: list[dict] = []

    def _capture_listing(**kwargs):
        listing_calls.append(kwargs)
        return [dict(row) for row in _PROVIDER_ROWS]

    monkeypatch.setattr("hermes_cli.model_switch_providers.list_picker_providers", _capture_listing)
    monkeypatch.setattr("hermes_cli.model_switch.list_authenticated_providers", _capture_listing)
    monkeypatch.setattr(
        "gateway.run._resolve_runtime_agent_kwargs_for_provider",
        lambda provider, target_model=None: {
            "provider": provider,
            "api_key": f"key-{provider}",
            "base_url": QWEN_URL if provider == CUSTOM else f"https://{provider}.test/v1",
            "api_mode": "chat_completions",
        },
    )

    adapter = _CapturingPickerAdapter() if picker else _TextOnlyAdapter()
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: adapter}
    # Launch config: no channel rule for company chats by default (the TGR-01 defect
    # shape) — the served company profile owns the qwen3.8-max/custom tier.
    runner.config = _typed_config(launch_overrides)
    runner._profile_configs = {
        "company": _typed_config(
            _company_channel_overrides() if company_overrides is None else company_overrides),
    }
    runner._voice_mode = {}
    runner._session_model_overrides = {}
    runner._running_agents = {}

    def _home_for(source):
        from gateway.session_identity import identity_of
        identity = identity_of(source)
        name = ((identity.runtime_profile if identity is not None else None)
                or getattr(source, "profile", None) or "default")
        return company_home if name == "company" else default_home

    runner._resolve_profile_home_for_source = _home_for
    runner._thread_metadata_for_source = lambda *a, **k: None
    runner._reply_anchor_for_event = lambda *a, **k: None
    return runner, adapter, listing_calls


class TestExposedParityCases:
    """The four actual exposed B1 cases: DM + forum x text-listing + picker."""

    @pytest.mark.asyncio
    async def test_company_dm_text_listing_shows_channel_tier(self, tmp_path, monkeypatch):
        runner, _adapter, _calls = _runner(tmp_path, monkeypatch, picker=False)
        event = _event(COMPANY_DM)
        _pin_company(event.source)

        reply = await runner._handle_model_command(event)

        assert reply is not None  # text-listing fallback path
        assert QWEN in reply  # the "Current:" line names the channel tier
        assert GLOBAL_MODEL not in reply  # never the profile global

    @pytest.mark.asyncio
    async def test_company_dm_picker_shows_channel_tier(self, tmp_path, monkeypatch):
        runner, adapter, listing_calls = _runner(tmp_path, monkeypatch, picker=True)
        event = _event(COMPANY_DM)
        _pin_company(event.source)

        assert await runner._handle_model_command(event) is None  # picker sent
        assert adapter.kwargs["current_model"] == QWEN
        assert adapter.kwargs["current_provider"] == CUSTOM
        # The picker's listing context carries the channel provider's own endpoint —
        # the same runtime resolution the turn performs
        # (run_turn.py::_resolve_session_agent_runtime).
        assert listing_calls[0]["current_provider"] == CUSTOM
        assert listing_calls[0]["current_base_url"] == QWEN_URL

    @pytest.mark.asyncio
    async def test_company_forum_text_listing_shows_channel_tier(self, tmp_path, monkeypatch):
        runner, _adapter, _calls = _runner(tmp_path, monkeypatch, picker=False)
        event = _event(COMPANY_FORUM, thread_id="20", chat_type="group")
        _pin_company(event.source)

        reply = await runner._handle_model_command(event)

        assert reply is not None
        assert QWEN in reply
        assert GLOBAL_MODEL not in reply

    @pytest.mark.asyncio
    async def test_company_forum_picker_shows_channel_tier(self, tmp_path, monkeypatch):
        runner, adapter, _calls = _runner(tmp_path, monkeypatch, picker=True)
        event = _event(COMPANY_FORUM, thread_id="21", chat_type="group")
        _pin_company(event.source)

        assert await runner._handle_model_command(event) is None  # picker sent
        assert adapter.kwargs["current_model"] == QWEN
        assert adapter.kwargs["current_provider"] == CUSTOM


class TestPrecedenceRegressions:
    """Session-pin precedence and warm-session consistency must survive the fix."""

    @pytest.mark.asyncio
    async def test_session_pin_beats_channel_tier(self, tmp_path, monkeypatch):
        runner, adapter, _calls = _runner(tmp_path, monkeypatch, picker=True)
        event = _event(COMPANY_DM)
        _pin_company(event.source)
        skey = runner._session_key_for_source(
            runner._normalize_source_for_session_key(event.source))
        runner._session_model_overrides[skey] = {
            "model": PINNED_MODEL, "provider": PINNED_PROVIDER,
        }

        assert await runner._handle_model_command(event) is None
        assert adapter.kwargs["current_model"] == PINNED_MODEL
        assert adapter.kwargs["current_provider"] == PINNED_PROVIDER

    @pytest.mark.asyncio
    async def test_warm_session_listing_matches_running_route(self, tmp_path, monkeypatch):
        # A warm session that already ran turns on the channel tier: /model must keep
        # advertising the route the warm session actually runs, never fall back to the
        # profile global.
        runner, _adapter, _calls = _runner(tmp_path, monkeypatch, picker=False)
        event = _event(COMPANY_FORUM, thread_id="20", chat_type="group")
        _pin_company(event.source)
        skey = runner._session_key_for_source(
            runner._normalize_source_for_session_key(event.source))
        runner._session_state(skey).conversation.last_resolved_model = QWEN

        reply = await runner._handle_model_command(event)

        assert reply is not None
        assert QWEN in reply
        assert GLOBAL_MODEL not in reply


class TestUnchangedBehavior:
    """A primary chat keeps its own (launch) rule — never borrows the served profile's."""

    @pytest.mark.asyncio
    async def test_primary_chat_picker_keeps_launch_rule(self, tmp_path, monkeypatch):
        # Trap: the company config claims the same chat id — an identity-less primary
        # source must get the launch rule, not the served profile's (TGR-01's isolation
        # guarantee, extended to the /model display).
        runner, adapter, _calls = _runner(
            tmp_path, monkeypatch, picker=True,
            launch_overrides={
                PRIMARY_CHAT: ChannelOverride(model="launch/model", provider="openrouter"),
            },
            company_overrides={
                **_company_channel_overrides(),
                PRIMARY_CHAT: ChannelOverride(model=QWEN, provider=CUSTOM),
            },
        )
        event = _event(PRIMARY_CHAT, chat_type="group")

        assert await runner._handle_model_command(event) is None
        assert adapter.kwargs["current_model"] == "launch/model"
        assert adapter.kwargs["current_provider"] == "openrouter"
