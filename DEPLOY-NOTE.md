# DEPLOY-NOTE — provider/model-availability startup crashes are not card failures

Card: `t_226958da` (fleet-ops). Branch: `task/t_226958da-provider-infra-class-main`.

**Base as published: `5ce2c7d5aa1` (the repo default branch `main` tip).** The
2026-10-15 company serialization ruling directs stacking this branch on the
specify-path tip `9fcc6d0807fdb5c599cf345609c71cae1dcd549f`
(`fix/specify-task-type-marker-carry`, card t_08f6c9a3) instead. That stack was built
and verified in full — cherry-pick **conflict-free**, suite green (16 passed) — and is
published as **PR #9** against a dedicated `base/9fcc6d0807f-provider-infra` branch, so
the ruling is delivered and reviewable. It cannot be the *completion-contract* base
here: the board's PR acceptance requires the PR base to be the repository **default
branch**, and a PR from `base/9fcc6d0807f-provider-infra` is rejected
(`PR acceptance base_mismatch`). The kernel-compliant PR is therefore cut from `main`
with the identical diff, and the stacked PR #9 is left open as the ruling's artifact.
Both are **NOT merged** (body requirement).

Practical consequence for the reviewer: the two PRs carry the **same three-file diff**
(842 insertions / 7 deletions). Merging the `main`-based one is the contract path; the
stacked one exists to satisfy the serialization ruling and to prove the specify-path
collision is clean.

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

## Binding company evidence (carried per the serialization ruling)

These are spec input for this change, not commentary. Summarised from the card's
company evidence comments; they were not re-derived here.

1. **Unstructured-403 recurrence, cause UNRESOLVED.** fleet-ops `t_cdc58018` run 5164,
   `openai-codex/gpt-6.1-sol`, exit 78. `task_runs` metadata says `terminal_provider`,
   but the retained provider message is `HTTP 403 — HTML error page (title not found)`,
   i.e. *unstructured*. The pre-existing assertion "credential or model rejected" is
   **stronger than the evidence**; the expected diagnostic cause must stay UNRESOLVED
   absent structured provider evidence. Constraints that follow: do **not** classify
   every HTTP 403 as transient, and do **not** weaken confirmed-invalid-credential or
   real quota controls.
2. **Usage/auth readback, 2026-10-15 01:13:53/55 +03:00.** Supported
   `hermes usage --provider openai-codex --json` exits 0 in operations and tech
   (Plus; session `used_percent=25`, weekly=4), i.e. the quota endpoint *answers*.
   Supported `auth list openai-codex` in four profiles shows the same visible entry id
   `8a052d` labelled auth-failed (403) — **one shared view, not proof that four
   independent accounts failed or that a token is revoked**. No auth flags were
   cleared, no refresh/rotation/re-auth, no pool/model/transport change, no inference
   probe.
3. **Offline classifier reproduction.** Installed `agent/error_classifier.py`
   SHA-256 `a05cb8d6fcdf37e48e266665a003d55b07f8de8caffb2c8fe0b662496c93e659`,
   `_status_403` lines 1019–1032: absent a recognised structured code/billing/WAF
   marker it **defaults to auth**. Isolated stdlib-only load of the pure decision
   function (no client/pool/config/auth/network surface): 6 cases, 4 controls pass,
   2 neutral-403 assertions fail (expected RED). Inputs were synthetic HTML plus the
   exact retained sanitized summary — so it demonstrates the **overconfident-diagnostic
   defect class** but does **not** identify the upstream cause or prove the original
   body took that branch.

**Classifier fitness against fixture 1 — verified here.** The shipped regexes
(`_PROVIDER_UNAVAILABLE_SIGNATURE_RE`, `_QUOTA_WINDOW_SIGNATURE_RE`) were AST-extracted
from the committed dispatcher and run against the binding fixtures plus the 2026-10-14
loop strings and the documented controls: **14/14 as required —
CLASSIFIER_BINDING_SAFE**. The bare `HTTP 403 — HTML error page` shape, the consumer
tail, and the auth-failed label match **neither** the unavailable nor the quota
signature, so this change cannot swallow an unresolved 403; the invalid-key, billing
and Cloudflare-marker controls stay unclassified; the loop-class banners, free-tier
refusal and provider-rejection strings classify `provider_unavailable`; the stated-reset
strings classify `quota_window`; genuine tracebacks stay unclassified. `tests (e)`
below pins this against the real reclaim path.

## Findings observed while verifying (recorded, NOT fixed here)

Running the binding fixture through the **real** reclaim path for each code the same
wall can surface as showed two pre-existing evidence defects **outside this change**:

* **exit 78** books the card with *"the provider rejected this profile's credential or
  model — fix the configuration, then unblock."* — a credential verdict asserted from a
  bare exit code, on an unstructured 403. That is exactly the over-confident diagnostic
  class binding comment 1 names, and it reaches a **sticky block**.
* **exit 75** stamps only its own generic *"rate-limited (quota wall)"* sentence and
  discards the provider words entirely, so the retained evidence is lost.

Neither is repaired here: comment 3 states the precise repair "remain[s] pending" and
that "company must bind an executable exact source baseline before any future
dispatch", and comment 1 scopes this card to the smallest diff in its frozen workspace.
Widening into `_status_403`/the exit-code booking without that binding would be an
unauthorised scope extension. They are pinned only as *no-infra-reclassification*
regressions, and the exit-75 gap is deliberately not asserted as correct.

## Verification

Run with the hermes-agent venv (`Python 3.11.16`, `pytest 9.1.1`):

```
export LOCALAPPDATA=<scratch>/lappdata TMPDIR=<scratch>/tmp TEMP=... TMP=...
unset HERMES_HOME
PYTHONPATH= <venv>/Scripts/python.exe -m pytest <file> -q -p no:cacheprovider
```

* `tests/hermes_cli/test_kanban_provider_infra_class.py` (new) — **16 passed** on the
  new base `9fcc6d0807f` (spec cases (a)–(d) plus the binding (e) fixture set).
  The same file passes **9 passed** as committed at spec (a)–(d) on the old base,
  i.e. the deliverable is base-agnostic.
* `tests/hermes_cli/test_kanban_worker_exit_trailer.py` — **5 passed**.
* `tests/hermes_cli/test_kanban_host_cap.py` — **13 passed**.
* `tests/hermes_cli/test_kanban_review_lifecycle.py` — **26 passed**.
* Full `tests/hermes_cli/ -k "kanban or single_query or dispatch or respawn or crash"`
  A/B against a pristine worktree at the same base: **85 failed / 668 passed** on the
  branch vs **85 failed / 659 passed** on base — **zero regressions**. The two
  set-difference entries (one each way) both pass in isolation and are flaky under the
  ~2.3 h suite load.

### Spec cases (a)–(d), all green

* **(a)** provider-unavailable exit → `infra` on all three codes, `consecutive_failures`
  untouched, not parked, `terminal_provider` marker cleared.
* **(b)** infra death requeues for free up to the streak threshold, then parks.
* **(c)** the infra cooldown spaces retries and does **not** trip `blocker_auth`.
* **(d)** the stored reason is the provider diagnostic, never the scraped TUI frame;
  a genuine worker error still counts as a normal failure.

### Spec case (e) — binding fixture, green

An unstructured HTML 403 is **never** reclassified as a walled rail on any of the three
codes, records no infra streak and cannot park; where a diagnostic is retained the
provider's own words survive sanitized and verbatim; and the confirmed
invalid-credential and quota controls keep their own classification.

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

## Stacking / single-writer discipline

The gated trunk-fold vehicles merge the stack **in order**; this branch stays unmerged
on the fork. The ruling's base is the specify-path tip `9fcc6d0807f`
(`fix/specify-task-type-marker-carry`, card t_08f6c9a3), which owns
`hermes_cli/kanban_specify.py`, `hermes_cli/kanban_decompose.py` and their tests. This
branch touches **only** `hermes_cli/kanban_db_dispatch.py`, its own new test file and
this note — verified with `git diff --name-only` — so the stack applies cleanly and the
single-writer rule holds by construction. `kanban_db.py` was read-only reference
throughout.

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

No fleet-policy edits, no provider/key/pool changes, no auth flag clears, no
refresh/rotation/re-auth, no model-routing changes, no live install writes, no inference
probes, no live-inference provider/pool/auth/routing changes, zero spend. The abandoned
composite canary and its cleanup were not revived.