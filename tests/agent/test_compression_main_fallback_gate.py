"""Owner no-fallback directive must also gate the COMPRESSOR's own one-shot main-model retry.

``auxiliary_client`` refuses to borrow the main agent model when
``auxiliary.<task>.main_agent_fallback: false`` meets an explicitly pinned aux provider. The
compressor owns a second, independent route to the main model: on any aux summary failure it clears
``summary_model`` and re-runs the summary on ``self.model`` (``_fallback_to_main_for_compression``).
That retry bypassed the client-level gate, so a pinned auxiliary rail still silently spent the main
agent model (live incident 10.10: a 401 on the aux model logged
"Falling back to main model 'qwen/qwen3.8-27b' for compression" despite the directive).
"""

from unittest.mock import MagicMock, patch

import pytest

# Warm the summary path's lazy ``agent.conversation_loop`` import at collection time: importing it
# mid-test pulls ``hermes_bootstrap``, whose import-time interrupted-update probe stats the REAL
# checkout's git dir and trips the per-test home-io guard (unrelated to the gate under test).
import agent.conversation_loop

from agent.context_compressor import ContextCompressor


class _AuthError(Exception):
    """A 401 from the aux provider — the class that rode the compressor's main-model fallback."""

    status_code = 401


def _msgs():
    return [
        {"role": "user", "content": "do something"},
        {"role": "assistant", "content": "ok"},
    ]


def _ok_response(text="summary via main model"):
    ok = MagicMock()
    ok.choices = [MagicMock()]
    ok.choices[0].message.content = text
    return ok


def _compressor():
    with patch("agent.context_compressor.get_model_context_length", return_value=100000):
        return ContextCompressor(model="main-model", quiet_mode=True)


def _aux_config(provider, main_agent_fallback=None):
    """A full config dict carrying only ``auxiliary.compression``."""
    compression = {"provider": provider, "model": "glm-5.3-flash"}
    if main_agent_fallback is not None:
        compression["main_agent_fallback"] = main_agent_fallback
    return {"auxiliary": {"compression": compression}}


def _failing_route(calls):
    """First call: aux route answers with a 401. Later calls: the main model succeeds."""

    def _route(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            kwargs["route_info"].update(provider="custom", model="glm-5.3-flash")
            raise _AuthError("Error code: 401 - invalid api key")
        kwargs["route_info"].update(provider="custom", model="main-model")
        return _ok_response()

    return _route


@pytest.mark.parametrize("flag", [False])
def test_pinned_provider_with_fallback_disabled_never_borrows_main_model(flag):
    """flag=false + explicit provider: no main-model summary request is issued."""
    calls = []
    c = _compressor()
    c.summary_model = "glm-5.3-flash"

    with patch("hermes_cli.config.load_config_readonly", return_value=_aux_config("custom", flag)):
        with patch("agent.context_compressor.call_llm", side_effect=_failing_route(calls)) as mock_call:
            result = c._generate_summary(_msgs())

    assert mock_call.call_count == 1, "the main-model retry must not fire"
    assert result is None
    # The failure still flows through the existing cooldown/flag machinery.
    assert c._last_summary_auth_failure is True
    assert c._summary_failure_cooldown_until > 0.0
    assert c._last_summary_error
    # ...and the aux route is left intact instead of being cleared for a main-model retry.
    assert c.summary_model == "glm-5.3-flash"
    assert getattr(c, "_summary_model_fallen_back", False) is False
    assert c._last_aux_model_failure_model is None


@pytest.mark.parametrize("flag", [True, None])
def test_pinned_provider_with_fallback_enabled_keeps_one_shot_retry(flag):
    """flag unset/true + explicit provider: the existing one-shot fallback is preserved."""
    calls = []
    c = _compressor()
    c.summary_model = "glm-5.3-flash"

    with patch("hermes_cli.config.load_config_readonly", return_value=_aux_config("custom", flag)):
        with patch("agent.context_compressor.call_llm", side_effect=_failing_route(calls)) as mock_call:
            result = c._generate_summary(_msgs())

    assert mock_call.call_count == 2
    # The retry must run on the MAIN model, not carry the failed aux model. On current HEAD the
    # handover travels as ``main_runtime``/``route_info`` (no bare ``model`` kwarg), so assert the
    # effective retry model instead of kwarg presence.
    retry_call = mock_call.call_args_list[1].kwargs
    retry_model = str(
        retry_call.get("model")
        or (retry_call.get("main_runtime") or {}).get("model")
        or (retry_call.get("route_info") or {}).get("model")
        or ""
    )
    assert retry_model == "main-model", f"retry model must be the main model, got {retry_model!r}"
    assert retry_model != "glm-5.3-flash"
    assert result is not None and "summary via main model" in result
    # The aux route was handed over to the main model and the failed model recorded for the warning.
    # (_summary_model_fallen_back is armed by the fallback but cleared again by the successful
    # summary — see _generate_summary's success path — so it is not the durable signal here.)
    assert c.summary_model == ""
    assert c._last_aux_model_failure_model == "glm-5.3-flash"


def test_auto_provider_is_never_gated_even_when_flag_is_false():
    """An auto/unset provider is not a pin: the directive must not disable the retry."""
    calls = []
    c = _compressor()

    with patch("hermes_cli.config.load_config_readonly", return_value=_aux_config("auto", False)):
        with patch("agent.context_compressor.call_llm", side_effect=_failing_route(calls)) as mock_call:
            result = c._generate_summary(_msgs())

    assert mock_call.call_count == 2
    assert result is not None and "summary via main model" in result


def test_gate_reads_only_the_compression_task_config():
    """The gate is task-scoped: a directive on another aux task cannot gate compression."""
    calls = []
    c = _compressor()
    c.summary_model = "glm-5.3-flash"
    cfg = {
        "auxiliary": {
            "compression": {"provider": "custom", "model": "glm-5.3-flash"},
            "vision": {"provider": "custom", "main_agent_fallback": False},
        }
    }

    with patch("hermes_cli.config.load_config_readonly", return_value=cfg):
        with patch("agent.context_compressor.call_llm", side_effect=_failing_route(calls)) as mock_call:
            result = c._generate_summary(_msgs())

    assert mock_call.call_count == 2
    assert result is not None