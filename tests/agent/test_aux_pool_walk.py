"""Same-provider pool walk: one bounded retry per eligible credential, identity-exact benching.

Live defect (28.10, gpt-free): after a rotation the ladder retried exactly once; when the
rotated-to credential ALSO failed, it benched that entry and fell through to the provider
fallback — the NEXT eligible account (21% used, healthy) was never tried in the same call
(agent.log 14:04:05: "rotated to gpt-free-3" then "Raising the primary error" 9ms later).
The retry-failure marking also dropped the failed-credential identity (no ``failed_api_key``),
so under a concurrent rotation the wrong — untried — entry could be benched (night 27.10
incident: a live account benched by a sibling's 429 window).
"""

from types import SimpleNamespace

import httpx
from openai import RateLimitError

from agent import auxiliary_client as aux


def _rl(message="Error code: 429 - usage limit reached"):
    return RateLimitError(
        message, response=httpx.Response(429, request=httpx.Request("POST", "https://pool.example/v1")),
        body=None,
    )


def _route(api_key):
    return SimpleNamespace(
        client=SimpleNamespace(api_key=api_key, base_url="https://pool.example/v1"),
        task="vision", tag=" (async)", resolved_provider="gpt-free",
        resolved_model="gpt-6-luna", final_model="gpt-6-luna",
        base_info="https://pool.example/v1", main_runtime=None,
    )


class _FakePool:
    """fill_first walk: ``recover`` models mark_exhausted_and_rotate — bench the hinted
    (actually-serving) entry, rotation succeeds while a non-benched entry remains."""

    def __init__(self, keys):
        self.eligible = list(keys)
        self.marked = []  # (failed_api_key hint, exc class name)

    def recover(self, provider, exc, *, failed_api_key=""):
        self.marked.append((failed_api_key, type(exc).__name__))
        if failed_api_key in self.eligible:
            self.eligible.remove(failed_api_key)
        return bool(self.eligible)


class _LoopPool(_FakePool):
    """A pathological rotation that never runs out — the walk must hit its safety cap."""

    def recover(self, provider, exc, *, failed_api_key=""):
        self.marked.append((failed_api_key, type(exc).__name__))
        return True


def _drive(generator, perform):
    """Minimal ladder driver: perform each yielded step; surface the generator return value.

    A performer failure is delivered with ``throw`` exactly like the production driver, so
    ``_rung``'s accept-check and the credential rungs' except handlers see real exceptions.
    """
    try:
        step = next(generator)
    except StopIteration as stop:
        return stop.value
    while True:
        try:
            outcome = perform(step)
        except Exception as exc:
            try:
                step = generator.throw(exc)
            except StopIteration as stop:
                return stop.value
        else:
            try:
                step = generator.send(outcome)
            except StopIteration as stop:
                return stop.value


def _ok_response():
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])


def test_two_exhausted_third_serves_in_same_call(monkeypatch):
    """#1 429 → rotate → #2 429 → rotate → #3 succeeds: one call, three credentials."""
    pool = _FakePool(["key-1", "key-2", "key-3"])
    monkeypatch.setattr(aux, "_recover_provider_pool", pool.recover)
    serving = iter(["key-2", "key-3"])
    attempts = []

    def perform(step):
        if step.kind == "call":  # the extra same-client retry on a non-payment 429
            attempts.append(("call", step.args[0].api_key))
            raise _rl()
        assert step.kind == "retry_same_provider", step
        key = next(serving)
        step.failed_key_box[:] = [key]  # the performer records the rebuilt client's key
        attempts.append(("retry", key))
        if key == "key-3":
            return _ok_response()
        raise _rl()

    result = _drive(aux._ladder_credential_rungs(_rl(), _route("key-1"), {}, False), perform)

    assert attempts == [("call", "key-1"), ("retry", "key-2"), ("retry", "key-3")]
    assert result[1] is None, "the healthy third account must serve the call"
    # Every bench is bound to the credential that actually served the failing request.
    assert pool.marked == [("key-1", "RateLimitError"), ("key-2", "RateLimitError")]
    assert "key-3" not in [hint for hint, _ in pool.marked], "the untried/successful account was benched"


def test_all_exhausted_walks_every_entry_exactly_once(monkeypatch):
    """All accounts 429: each eligible entry is tried once, marked with its own key, then stop."""
    pool = _FakePool(["key-1", "key-2", "key-3"])
    monkeypatch.setattr(aux, "_recover_provider_pool", pool.recover)
    attempts = []

    def perform(step):
        if step.kind == "call":
            attempts.append(("call", step.args[0].api_key))
        else:
            key = f"key-{sum(1 for a in attempts if a[0] == 'retry') + 2}"  # retry #1 serves key-2, #2 serves key-3
            step.failed_key_box[:] = [key]
            attempts.append(("retry", key))
        raise _rl()

    result = _drive(aux._ladder_credential_rungs(_rl(), _route("key-1"), {}, False), perform)

    assert [a[0] for a in attempts] == ["call", "retry", "retry"], "one retry per remaining entry"
    assert result[0] is None, "exhausted pool must fail, not loop"
    assert pool.marked == [("key-1", "RateLimitError"), ("key-2", "RateLimitError"),
                           ("key-3", "RateLimitError")]


def test_walk_is_capped_against_pathological_rotation(monkeypatch):
    """A rotation that never runs dry must not spin the ladder forever."""
    pool = _LoopPool(["key-1", "key-2", "key-3"])
    monkeypatch.setattr(aux, "_recover_provider_pool", pool.recover)
    attempts = []

    def perform(step):
        if step.kind == "call":
            attempts.append("call")
        else:
            step.failed_key_box[:] = [f"key-{len(attempts)}"]
            attempts.append("retry")
        raise _rl()

    result = _drive(aux._ladder_credential_rungs(_rl(), _route("key-1"), {}, False), perform)

    assert result[0] is None
    retries = attempts.count("retry")
    assert retries <= aux._POOL_WALK_MAX_STEPS, f"unbounded walk: {retries} retries"
