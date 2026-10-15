# DEPLOY-NOTE — provider/model-availability startup crashes are not card failures

Card: `t_226958da` (fleet-ops). Branch: `task/t_226958da-provider-infra-class`.
Base: `5ce2c7d5aa15741da3569bea0fc693629b634ce5`. PR to `maximalang/hermes-agent`, **NOT merged**.

## Problem

A worker that dies at startup because its model/provider is unavailable
(`provider=custom model=qwen-3.8-max`, "model is not available") was booked as an
ordinary card failure. The dispatcher scraped the final ANSI TUI frame into
`last_failure_error`, incremented `consecutive_failures`, and pushed healthy cards
toward `gave_up`. Four loop reviews on 2026-10-14 (`t_fcc571ae`, `t_8e3623e9`,
`t_c8fa0f13`, `t_3a302dee`) show byte-identical startup banners while the same rail
answered OK minutes later — a 33-card loop class inflated by a transient
shared-key rejection. The card is **not** defective: nothing in its body ever ran.

## Change (one file, `hermes_cli/kanban_db_dispatch.py`, +267/-7)

New neutral run outcome **`infra`**, disjoint from both `rate_limited` and `crashed`:

1. **Classification keys off the worker's own log text, not the exit code.** The same
   provider wall surfaces as exit **1** (unhandled), **75** (`rate_limited`) or **78**
   (`terminal_provider`) depending on where it was noticed, so `_infra_diagnostic`
   matches the provider-availability sentence in the worker log/trailer instead.
2. **Neutral accounting.** An `infra` death records its own run/event but never touches
   `consecutive_failures` and can never produce `gave_up`.
3. **ANSI-stripped diagnostic.** `_strip_ansi` / `_sanitize_diagnostic` store the
   provider's own words in `last_failure_error`, never a scraped TUI frame.
4. **Bounded respawn.** Below the threshold the card requeues on the ordinary
   rate-limit cooldown (`infra_cooldown`). After `_INFRA_RAIL_MAX_CONSECUTIVE` (3)
   consecutive infra deaths, `_park_infra_rail` parks the card through the canonical
   `kanban_db.block_task` with an explicit `infra` marker, so the block is **sticky**
   (a hand-rolled status flip plus a custom event kind would be promoted straight back
   to `ready` by `recompute_ready`) and releasable with ordinary `kanban_unblock`.
5. **`check_respawn_guard` early-returns on `outcome == "infra"`** so the stamped
   provider diagnostic — which legitimately contains "quota"/"rate limit" — cannot
   match `_RESPAWN_BLOCKER_RE` and trap a healthy card forever.
6. **No `last_failure_error` duplication.** The worker log is opened in append mode, so
   a retry re-feeds the previous attempt's sentence. The diagnostic builder dedupes
   lines (case-folded) so the stored reason does not grow a copy of itself per retry.

## Verification

Run with the hermes-agent venv (`Python 3.11.16`, `pytest 9.1.1`):

```
export LOCALAPPDATA=<scratch>/lappdata TMPDIR=<scratch>/tmp TEMP=… TMP=…
unset HERMES_HOME
PYTHONPATH= <venv>/Scripts/python.exe -m pytest <file> -q -p no:cacheprovider
```

* `tests/hermes_cli/test_kanban_provider_infra_class.py` (new, spec cases (a)–(d)) —
  **9 passed**.
* `tests/hermes_cli/test_kanban_worker_exit_trailer.py` — **5 passed**.
* `tests/hermes_cli/test_kanban_host_cap.py` — **13 passed**.
* `tests/hermes_cli/test_kanban_review_lifecycle.py` — **26 passed**.
* Full `tests/hermes_cli/ -k "kanban or single_query or dispatch or respawn or crash"`
  A/B against a pristine worktree at the same base: **85 failed / 668 passed** on the
  branch vs **85 failed / 659 passed** on base — **zero regressions**. The two
  set-difference entries (one each way) both pass in isolation and are flaky under the
  ~2.3 h suite load.

### Acceptance case (a) — pre-existing, platform-gated

`tests/hermes_cli/test_kanban_db.py::test_infrastructure_spawn_refusal_never_charges_the_card`
fails **identically on base and on this branch**. It is not a gap in this change: it
drives the *host spawn-refusal* class (#114720, `RestartSafeScopeUnavailable` from the
real systemd boundary), a different trigger from this card's provider-availability
spec — and `tools/process_registry.py:420` short-circuits `if not _IS_LINUX: return
GatewayChildDispatch("in_process", command)`, with `_IS_LINUX = False` on this Windows
host, so the real boundary can never raise here. Verified: `IS_LINUX= False`.

This change is **disjoint** from that class: it adds the provider-availability path and
performs no edits to the spawn-refusal accounting.

## Disjointness (single-writer rule)

The card bans overlap with `codex/specify-path-hardening` (t_08f6c9a3), which owns
`gateway/kanban_watchers_dispatcher.py`, `hermes_cli/kanban_db.py`,
`hermes_cli/kanban_decompose.py`. This branch touches **only**
`hermes_cli/kanban_db_dispatch.py` plus its own new test file — verified with
`git diff --name-only`. `kanban_db.py` was read-only reference throughout.

Note for the reviewer: open PR **#6** (`fix/infra-kill-requeue-t_1b5e3e56`, card
t_1b5e3e56) also edits `kanban_db_dispatch.py`. It is **complementary, not duplicate**:
PR #6 classifies dispatcher-shutdown *drain* deaths via an `infra-drain.json` marker
and matches on task+pid+fingerprint; this change classifies *provider/model
unavailability* from the worker's own log. A merge must reconcile both in the same
function region (`_record_task_failure` / `detect_crashed_workers` bucketing).

## Rollback

Single-file, additive diff — `git revert <sha>` (or drop the commit) restores the prior
behaviour: `infra` deaths fall back to the ordinary `crashed` path. No schema migration
is involved (`task_runs.outcome` has no CHECK constraint; the only `outcome ==`
comparison in `kanban_db.py:4130` tests `"completed"`). Nothing was installed or
deployed; the live install was read-only reference only. Zero spend.

## Bans honoured

No fleet-policy edits, no provider/key/pool changes, no model-routing changes, no live
install writes, zero spend.