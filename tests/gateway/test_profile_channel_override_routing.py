"""TGR-01: routed multiplex profile channel model/provider selection.

Regression matrix for the defect where ``GatewayRunner.config`` — the LAUNCH default
profile's typed config — was the only source consulted for ``channel_overrides``
lookups, so a secondary bot profile's native per-channel model/provider rules were
silently ignored: fresh company Telegram DM/forum topics resolved the launch
profile's global model (Sol) instead of the routed profile's rule
(qwen3.8-max/custom).

The fix resolves the routed profile's typed config (``_profile_configs``) via
``_channel_override_config_for_source`` at the two live lookup sites — turn runtime
(``_resolve_session_agent_runtime``) and the slash/status pin logic
(``_channel_override_for``). Resolution order mirrors
``_resolve_profile_home_for_source``: pinned ``RoutingIdentity`` → ``source.profile``
→ configured routes; every miss (standalone, primary, unserved/unknown profile,
route rejection) falls back to the launch config, never to another profile's rules.

All provider resolution is mocked; no network, no live config, no credentials.
"""

from pathlib import Path
from unittest.mock import patch

from gateway.config import (
    ChannelOverride,
    GatewayConfig,
    Platform,
    PlatformConfig,
)
from gateway.profile_routing import ProfileRouteRejected
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.session_identity import RoutingIdentity

COMPANY_DM = "1256122537"
COMPANY_FORUM = "-1004426332349"
QWEN = "qwen3.8-max"
SOL = "gpt-5.6-sol"


def _identity(runtime_profile: str, *, multiplexed: bool = True) -> RoutingIdentity:
    return RoutingIdentity(
        transport_profile="default",
        runtime_profile=runtime_profile,
        authorization_home=Path("."),
        runtime_home=Path("."),
        multiplexed=multiplexed,
    )


def _telegram_config(*, multiplex: bool = True, overrides: dict | None = None) -> GatewayConfig:
    return GatewayConfig(
        multiplex_profiles=multiplex,
        platforms={
            Platform.TELEGRAM: PlatformConfig(
                enabled=True,
                channel_overrides=dict(overrides or {}),
            ),
        },
    )


def _runner(launch_cfg: GatewayConfig, profile_configs: dict | None = None) -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner._session_model_overrides = {}
    runner.config = launch_cfg
    if profile_configs is not None:
        runner._profile_configs = dict(profile_configs)
    return runner


def _telegram_source(chat_id: str, *, thread_id: str | None = None) -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        user_id="u1",
        thread_id=thread_id,
    )


def _pin(source: SessionSource, runtime_profile: str, *, multiplexed: bool = True) -> SessionSource:
    setattr(source, "_identity", _identity(runtime_profile, multiplexed=multiplexed))
    return source


def _resolve_runtime(runner: GatewayRunner, source: SessionSource, *, session_key: str | None = None):
    """One turn-runtime model/runtime resolution with every provider lookup mocked."""
    provider_calls: list[tuple] = []

    def _for_provider(provider, target_model=None):
        provider_calls.append((provider, target_model))
        return {
            "provider": provider,
            "api_key": f"key-{provider}",
            "base_url": f"https://{provider}.test/v1",
            "api_mode": "chat_completions",
        }

    # The launch profile's default route: Sol on the default provider — the wrong
    # route a routed company topic must NOT inherit.
    default_kwargs = {
        "provider": "openai-codex",
        "api_key": "key-sol",
        "base_url": "https://sol.test",
        "api_mode": "chat_completions",
        "model": SOL,
    }
    with patch("gateway.run._resolve_gateway_model", return_value=SOL), \
         patch("gateway.run._resolve_runtime_agent_kwargs", return_value=dict(default_kwargs)), \
         patch("gateway.run._resolve_runtime_agent_kwargs_for_provider", side_effect=_for_provider), \
         patch("gateway.run._credential_pool_for_provider", return_value=None):
        model, runtime = runner._resolve_session_agent_runtime(
            source=source,
            session_key=session_key,
            user_config={"model": {"default": SOL}},
        )
    return model, runtime, provider_calls


class TestRoutedTurnRuntime:
    """Turn runtime must apply the ROUTED profile's channel rule (model AND provider)."""

    def test_company_dm_fresh_topic_uses_company_rule(self):
        launch = _telegram_config()  # no channel rule for the company DM; global default = Sol
        company = _telegram_config(
            overrides={COMPANY_DM: ChannelOverride(model=QWEN, provider="custom")})
        runner = _runner(launch, {"company": company})
        source = _pin(_telegram_source(COMPANY_DM), "company")

        model, runtime, provider_calls = _resolve_runtime(runner, source)

        assert model == QWEN
        # Provider asserted separately: the Qwen name must travel on the custom
        # provider's runtime, never Sol's default route.
        assert runtime["provider"] == "custom"
        assert ("custom", QWEN) in provider_calls

    def test_company_forum_fresh_topic_uses_company_rule(self):
        launch = _telegram_config()
        company = _telegram_config(
            overrides={COMPANY_FORUM: ChannelOverride(model=QWEN, provider="custom")})
        runner = _runner(launch, {"company": company})
        source = _pin(_telegram_source(COMPANY_FORUM, thread_id="20"), "company")

        model, runtime, provider_calls = _resolve_runtime(runner, source)

        assert model == QWEN
        assert runtime["provider"] == "custom"
        assert ("custom", QWEN) in provider_calls

    def test_subsequent_fresh_topic_inherits_company_rule(self):
        launch = _telegram_config()
        company = _telegram_config(
            overrides={COMPANY_FORUM: ChannelOverride(model=QWEN, provider="custom")})
        runner = _runner(launch, {"company": company})

        for thread in ("20", "21"):
            source = _pin(_telegram_source(COMPANY_FORUM, thread_id=thread), "company")
            model, runtime, _ = _resolve_runtime(runner, source)
            assert (model, runtime["provider"]) == (QWEN, "custom"), f"thread {thread}"

    def test_specialist_profile_uses_own_rule_not_company_or_launch(self):
        chat = "chatX"
        launch = _telegram_config(overrides={chat: ChannelOverride(model="launch/model")})
        company = _telegram_config(overrides={chat: ChannelOverride(model=QWEN, provider="custom")})
        design = _telegram_config(
            overrides={chat: ChannelOverride(model="design/model", provider="openrouter")})
        runner = _runner(launch, {"company": company, "design": design})
        source = _pin(_telegram_source(chat), "design")

        model, runtime, _ = _resolve_runtime(runner, source)

        assert model == "design/model"
        assert runtime["provider"] == "openrouter"


class TestUnchangedBehavior:
    """Standalone, primary and session-pin behavior must be byte-identical."""

    def test_standalone_uses_launch_config(self):
        launch = _telegram_config(
            multiplex=False,
            overrides={COMPANY_DM: ChannelOverride(model="launch/model", provider="openrouter")})
        runner = _runner(launch)
        source = _telegram_source(COMPANY_DM)

        model, runtime, _ = _resolve_runtime(runner, source)

        assert model == "launch/model"
        assert runtime["provider"] == "openrouter"

    def test_primary_chat_in_multiplex_uses_launch_config(self):
        chat = "primary-desktop-chat"
        launch = _telegram_config(
            overrides={chat: ChannelOverride(model="launch/model", provider="openrouter")})
        # Trap: a served profile claims the same chat id — a primary source (no
        # identity, no profile, no route) must never borrow it.
        company = _telegram_config(overrides={chat: ChannelOverride(model=QWEN, provider="custom")})
        runner = _runner(launch, {"company": company})
        source = _telegram_source(chat)

        model, runtime, _ = _resolve_runtime(runner, source)

        assert model == "launch/model"
        assert runtime["provider"] == "openrouter"

    def test_session_pin_beats_channel_rule(self):
        company = _telegram_config(
            overrides={COMPANY_DM: ChannelOverride(model=QWEN, provider="custom")})
        runner = _runner(_telegram_config(), {"company": company})
        source = _pin(_telegram_source(COMPANY_DM), "company")
        skey = "agent:company:telegram:dm1"
        runner._session_state(skey).conversation.model_override = {
            "model": "pinned/model",
            "provider": "openai-codex",
            "api_key": "sk-pin",
            "base_url": "https://pin.test",
        }

        model, runtime, provider_calls = _resolve_runtime(runner, source, session_key=skey)

        assert model == "pinned/model"
        assert runtime["provider"] == "openai-codex"
        # Fast path: the session /model pin returns before any channel/provider
        # resolution — the explicit pin stays the highest priority.
        assert provider_calls == []

    def test_warm_cache_signature_changes_with_route(self):
        company = _telegram_config(
            overrides={COMPANY_DM: ChannelOverride(model=SOL, provider="openai-codex")})
        runner = _runner(_telegram_config(), {"company": company})
        source = _pin(_telegram_source(COMPANY_DM), "company")

        model1, rt1, _ = _resolve_runtime(runner, source)
        sig1 = GatewayRunner._agent_config_signature(model1, rt1, [], "")

        # A legitimate routed config change (company rule now Qwen) must produce a
        # different agent-cache signature so the warm agent is rebuilt, never
        # carrying the old Sol route.
        company.platforms[Platform.TELEGRAM].channel_overrides[COMPANY_DM] = (
            ChannelOverride(model=QWEN, provider="custom"))

        model2, rt2, _ = _resolve_runtime(runner, source)
        sig2 = GatewayRunner._agent_config_signature(model2, rt2, [], "")

        assert (model1, rt1["provider"]) == (SOL, "openai-codex")
        assert (model2, rt2["provider"]) == (QWEN, "custom")
        assert sig1 != sig2


class TestConfigHelper:
    """``_channel_override_config_for_source``: routed config or safe launch fallback."""

    def test_routed_identity_returns_profile_config(self):
        launch = _telegram_config()
        company = _telegram_config()
        runner = _runner(launch, {"company": company})
        source = _pin(_telegram_source(COMPANY_DM), "company")

        assert runner._channel_override_config_for_source(source) is company

    def test_source_profile_fallback_returns_profile_config(self):
        launch = _telegram_config()
        company = _telegram_config()
        runner = _runner(launch, {"company": company})
        source = _telegram_source(COMPANY_DM)
        source.profile = "company"

        assert runner._channel_override_config_for_source(source) is company

    def test_route_resolver_result_returns_profile_config(self):
        launch = _telegram_config()
        company = _telegram_config()
        runner = _runner(launch, {"company": company})
        runner._profile_name_for_source = lambda src: "company"
        source = _telegram_source(COMPANY_DM)

        assert runner._channel_override_config_for_source(source) is company

    def test_unknown_profile_falls_back_to_launch(self):
        launch = _telegram_config()
        runner = _runner(launch, {"company": _telegram_config()})
        source = _telegram_source(COMPANY_DM)
        source.profile = "ghost"  # not served / not cached

        assert runner._channel_override_config_for_source(source) is launch

    def test_route_rejection_falls_back_to_launch(self):
        launch = _telegram_config()
        runner = _runner(launch, {"company": _telegram_config()})

        def _rejected(src):
            raise ProfileRouteRejected("ghost")

        runner._profile_name_for_source = _rejected
        source = _telegram_source("999")

        assert runner._channel_override_config_for_source(source) is launch

    def test_standalone_returns_launch_even_with_identity(self):
        launch = _telegram_config(multiplex=False)
        company = _telegram_config()
        runner = _runner(launch, {"company": company})
        source = _pin(_telegram_source(COMPANY_DM), "company")

        assert runner._channel_override_config_for_source(source) is launch

    def test_missing_profile_cache_returns_launch(self):
        launch = _telegram_config()
        runner = _runner(launch)  # no _profile_configs attribute at all
        source = _pin(_telegram_source(COMPANY_DM), "company")

        assert runner._channel_override_config_for_source(source) is launch


class TestSlashChannelOverrideFor:
    """/model's channel-override lookup must agree with the turn runtime."""

    def test_routed_profile_sees_company_rule(self):
        launch = _telegram_config()  # no rule -> base returns None here (the defect)
        company = _telegram_config(
            overrides={COMPANY_DM: ChannelOverride(model=QWEN, provider="custom")})
        runner = _runner(launch, {"company": company})
        source = _pin(_telegram_source(COMPANY_DM), "company")

        override = runner._channel_override_for(source)

        assert override is not None
        assert override.model == QWEN
        assert override.provider == "custom"

    def test_primary_source_keeps_launch_rule(self):
        launch = _telegram_config(
            overrides={COMPANY_DM: ChannelOverride(model="launch/model", provider="openrouter")})
        runner = _runner(launch, {"company": _telegram_config()})
        source = _telegram_source(COMPANY_DM)

        override = runner._channel_override_for(source)

        assert override is not None
        assert override.model == "launch/model"
