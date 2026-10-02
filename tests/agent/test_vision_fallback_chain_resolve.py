"""Regression tests for the 02.10 vision fallback incident (fleet-ops t_7e18e350).

Defects covered (agent/auxiliary_client.py):
1. The vision branch of ``_resolve_call_client`` skipped the task's
   ``auxiliary.vision.fallback_chain`` when an explicit provider could not
   build a client — unlike the non-vision branch — and jumped straight to
   auto-detect.
2. That jump carried the unavailable provider's model name into the auto
   route, forcing a foreign model (codex ``gpt-6.1-sol``) onto the main
   provider's endpoint (DashScope) → 404 model_not_found.
3. A 404 ``model_not_found`` had no ``_FALLBACK_REASONS`` entry, so the
   recovery ladder computed ``reason=None`` and re-raised without ever
   consulting the configured task chain.

Style mirrors tests/agent/test_vision_routing.py: isolated HERMES_HOME,
fresh module reload per test, mocks only — no network, no real accounts.

Chain fixtures use the owner's canonical vision fallback (owner directive
02.10, config-backups/vision-luna-20261002T084037Z): auxiliary.vision =
openai-codex/gpt-6-luna with fallback_chain [custom/qwen-vl-max]. Free-tier
openrouter vision models are prohibited by the owner and must not appear here.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Test infrastructure (mirrors test_vision_routing.py)
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_home(monkeypatch):
    """Temp HERMES_HOME with config + clean auth env vars."""
    test_home = tempfile.mkdtemp(prefix="hermes_test_vfix_")
    hermes_home = os.path.join(test_home, ".hermes")
    os.makedirs(hermes_home)
    monkeypatch.setenv("HERMES_HOME", hermes_home)
    for k in list(os.environ.keys()):
        if k.endswith("_API_KEY") or k.endswith("_TOKEN"):
            monkeypatch.delenv(k, raising=False)
    yield hermes_home
    shutil.rmtree(test_home, ignore_errors=True)


def _write_config(home: str, text: str) -> None:
    with open(os.path.join(home, "config.yaml"), "w") as fp:
        fp.write(text)


_RELOAD_PREFIXES = ("agent.auxiliary_client", "hermes_cli.config",
                    "hermes_cli.fallback_config")


def _drop_reload_targets():
    for mod in list(sys.modules.keys()):
        if mod.startswith(_RELOAD_PREFIXES):
            del sys.modules[mod]


@pytest.fixture(autouse=True)
def _module_isolation():
    """Save/restore sys.modules entries this file reloads (issue #61597)."""
    saved = {name: mod for name, mod in sys.modules.items()
             if name.startswith(_RELOAD_PREFIXES)}
    yield
    _drop_reload_targets()
    sys.modules.update(saved)


def _fresh_modules():
    _drop_reload_targets()


_CONFIG_WITH_CHAIN = """
auxiliary:
  vision:
    provider: openai-codex
    model: gpt-6-luna
    fallback_chain:
      - provider: custom
        model: qwen-vl-max
"""

_CONFIG_NO_CHAIN = """
auxiliary:
  vision:
    provider: openai-codex
    model: gpt-6-luna
"""


def _call_resolve(ac, *, async_mode=False):
    """Invoke _resolve_call_client the way _prepare_aux_request does after
    _resolve_task_provider_model resolved auxiliary.vision config for an
    explicit provider with no direct-endpoint override."""
    return ac._resolve_call_client(
        "vision", provider=None, model=None, base_url=None, api_key=None,
        resolved_provider="openai-codex", resolved_model="gpt-6-luna",
        resolved_base_url=None, resolved_api_key=None, resolved_api_mode=None,
        main_runtime=None, async_mode=async_mode)


def _model_not_found_404() -> Exception:
    """The incident error shape: DashScope 404 for a foreign (codex) model."""
    exc = Exception(
        "Error code: 404 - {'error': {'message': 'The model `gpt-6-luna` does not exist', "
        "'type': 'invalid_request_error', 'param': None, 'code': 'model_not_found'}}")
    exc.status_code = 404
    return exc


# ---------------------------------------------------------------------------
# Defect 1: explicit vision provider unavailable → task fallback_chain first
# ---------------------------------------------------------------------------

class TestVisionTaskChainBeforeAuto:

    def test_explicit_provider_unavailable_uses_task_chain(self, isolated_home):
        """Pool-exhaustion scenario (09:13 02.10): no pool entries for the
        explicit provider → client None → the canonical chain
        [custom/qwen-vl-max] must serve the call without touching auto-route."""
        _write_config(isolated_home, _CONFIG_WITH_CHAIN)
        _fresh_modules()
        from agent import auxiliary_client as ac

        chain_client = MagicMock(name="chain-client")
        rpcc = MagicMock(return_value=("openai-codex", None, None))
        auto = MagicMock(name="auto-route-must-not-run")
        with patch.object(ac, "resolve_vision_provider_client", rpcc), \
             patch.object(ac, "_resolve_fallback_entry",
                          return_value=(chain_client, "qwen-vl-max")), \
             patch.object(ac, "_is_provider_unhealthy", return_value=False), \
             patch.object(ac, "_vision_auto_route", auto):
            route = _call_resolve(ac)

        assert route.client is chain_client, (
            "the configured fallback_chain provider must serve the call when the "
            "explicit vision provider cannot build a client")
        assert route.final_model == "qwen-vl-max"
        assert route.resolved_provider == "fallback_chain[0](custom)"
        assert route.effective_provider == "fallback_chain[0](custom)"
        auto.assert_not_called()
        assert rpcc.call_count == 1, "no auto retry through resolve_vision_provider_client"

    def test_chain_client_wrapped_for_async_vision(self, isolated_home):
        _write_config(isolated_home, _CONFIG_WITH_CHAIN)
        _fresh_modules()
        from agent import auxiliary_client as ac

        chain_client = MagicMock(name="chain-client")
        async_client = MagicMock(name="async-chain-client")
        to_async = MagicMock(return_value=(async_client, "qwen-vl-max"))
        with patch.object(ac, "resolve_vision_provider_client",
                          MagicMock(return_value=("openai-codex", None, None))), \
             patch.object(ac, "_resolve_fallback_entry",
                          return_value=(chain_client, "qwen-vl-max")), \
             patch.object(ac, "_is_provider_unhealthy", return_value=False), \
             patch.object(ac, "_to_async_client", to_async):
            route = _call_resolve(ac, async_mode=True)

        assert route.client is async_client
        assert route.final_model == "qwen-vl-max"
        to_async.assert_called_once_with(
            chain_client, "qwen-vl-max", is_vision=True)


# ---------------------------------------------------------------------------
# Defect 2: chain empty/exhausted → auto-route with model=None
# ---------------------------------------------------------------------------

class TestAutoRouteNeverCarriesForeignModel:

    def test_empty_chain_auto_routes_without_foreign_model(self, isolated_home):
        _write_config(isolated_home, _CONFIG_NO_CHAIN)
        _fresh_modules()
        from agent import auxiliary_client as ac

        auto_client = MagicMock(name="auto-client")
        auto = MagicMock(return_value=("custom", auto_client, "qwen3.6-plus"))
        rpcc = MagicMock(return_value=("openai-codex", None, None))
        with patch.object(ac, "resolve_vision_provider_client", rpcc), \
             patch.object(ac, "_vision_auto_route", auto):
            route = _call_resolve(ac)

        assert route.client is auto_client
        assert route.final_model == "qwen3.6-plus"
        assert route.final_model != "gpt-6-luna", (
            "the unavailable provider's model name must never ride along to "
            "another endpoint")
        assert route.resolved_provider == "custom"
        args, _kwargs = auto.call_args
        assert isinstance(args[0], dict), "normalized main runtime passed positionally"
        assert args[1] is None, "auto-route must run with model=None (own defaults)"
        assert rpcc.call_count == 1, "auto retry must not re-enter resolve_vision_provider_client"


# ---------------------------------------------------------------------------
# Regression: an available explicit provider is untouched by the new path
# ---------------------------------------------------------------------------

class TestAvailableExplicitProviderUnchanged:

    def test_available_provider_skips_chain_and_auto(self, isolated_home):
        _write_config(isolated_home, _CONFIG_WITH_CHAIN)
        _fresh_modules()
        from agent import auxiliary_client as ac

        client = MagicMock(name="explicit-client")
        rpcc = MagicMock(return_value=("openai-codex", client, "gpt-6-luna"))
        chain = MagicMock(name="chain-must-not-run")
        auto = MagicMock(name="auto-must-not-run")
        with patch.object(ac, "resolve_vision_provider_client", rpcc), \
             patch.object(ac, "_try_configured_fallback_for_unavailable_client", chain), \
             patch.object(ac, "_vision_auto_route", auto):
            route = ac._resolve_call_client(
                "vision", provider=None, model=None, base_url=None, api_key=None,
                resolved_provider="openai-codex", resolved_model="gpt-6-luna",
                resolved_base_url=None, resolved_api_key=None, resolved_api_mode=None,
                main_runtime=None, async_mode=False)

        assert route.client is client
        assert route.final_model == "gpt-6-luna"
        assert route.resolved_provider == "openai-codex"
        assert route.effective_provider == "openai-codex"
        chain.assert_not_called()
        auto.assert_not_called()
        rpcc.assert_called_once()


# ---------------------------------------------------------------------------
# Defect 3: 404 model_not_found reaches the provider-fallback rung
# ---------------------------------------------------------------------------

class TestModelNotFoundReachesProviderFallback:

    def test_fallback_reasons_label_model_not_found(self, isolated_home):
        _fresh_modules()
        from agent import auxiliary_client as ac
        reason = next((label for pred, label in ac._FALLBACK_REASONS
                       if pred(_model_not_found_404())), None)
        assert reason == "model not found", (
            "a 404 model_not_found must map to a ladder reason, or "
            "_ladder_provider_fallback returns None without consulting any chain")

    def test_billing_lookalike_is_not_model_not_found(self, isolated_home):
        """Payment-flavoured 404 bodies stay owned by the earlier rungs."""
        _fresh_modules()
        from agent import auxiliary_client as ac
        exc = Exception(
            "Error code: 404 - The model `x` does not exist (no usable credits)")
        exc.status_code = 404
        reason = next((label for pred, label in ac._FALLBACK_REASONS
                       if pred(exc)), None)
        assert reason != "model not found"

    def test_param_rung_falls_through_on_model_not_found(self, isolated_home):
        """After a parameter-strip retry, a 404 model_not_found continues down
        the ladder instead of being re-raised from the param rung."""
        _fresh_modules()
        from agent import auxiliary_client as ac
        assert ac._param_rung_accepts(_model_not_found_404()) is True

    def test_ladder_consults_task_chain_on_model_not_found(self, isolated_home):
        _write_config(isolated_home, """
auxiliary:
  vision:
    provider: custom
    model: qwen-vl-max
    fallback_chain:
      - provider: custom
        model: qwen-vl-max
""")
        _fresh_modules()
        from agent import auxiliary_client as ac

        chain_client = MagicMock(name="chain-client")
        steps = []

        def perform(step):
            steps.append(step)
            return "candidate-response"

        dashscope = "https://dashscope.aliyuncs.com/compatible-mode/v1"
        client = SimpleNamespace(base_url=dashscope, api_key="")
        with patch.object(ac, "_resolve_fallback_entry",
                          return_value=(chain_client, "qwen-vl-max")), \
             patch.object(ac, "_is_provider_unhealthy", return_value=False):
            ladder = ac._aux_recovery_ladder(
                _model_not_found_404(), client=client, kwargs={}, task="vision",
                async_mode=False, base_info=dashscope, resolved_provider="custom",
                resolved_model="qwen3.6-plus", resolved_base_url=dashscope,
                resolved_api_key=None, resolved_api_mode="chat_completions",
                final_model="gpt-6-luna", max_tokens=None, main_runtime=None,
                route_info=None)
            resp = ac._drive_ladder(ladder, perform)

        assert resp == "candidate-response"
        assert len(steps) == 1, "exactly one fallback step, no earlier rung fires"
        step = steps[0]
        assert step.kind == "fallback"
        assert step.args[0] is chain_client
        assert step.args[1] == "qwen-vl-max"
        assert step.args[2] == "fallback_chain[0](custom)"
