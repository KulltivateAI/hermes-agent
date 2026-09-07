# Runtime reliability repair — PR1 release checkpoint

## Authority and release boundary

- Canonical contract: KulltivateAI `origin/main:prd/2026-09-07-runtime-reliability-PRD.md`.
- Intake `t_92bb1b12`; PR1 `t_8a7ed41b`, accepted run `5381`; PR2 `t_2954b347` depends on PR1.
- Base and rollback source: `869228cab4a8276d3b4c78da9d9939670c47bd0f`.
- Branch `repair/runtime-reliability-state` targets `review-base/runtime-reliability-869228c` in `KulltivateAI/hermes-agent`, NOT fork main or upstream latest.
- Author checkout: `/Users/kulltivate/code/hermes-runtime-reliability`. Installed source and old repair WIP were not modified.
- Independent verdict / reviewed head / merge SHA: **pending Konsult**. Exact candidate head and hosted receipts are recorded in the PR/check run and final handoff; this manifest is part of that head.
- PR1 is a dependency, **not an activation target or a shipped repair**. PR2 starts only after independently reviewed PR1 merge, in this same clean author worktree on `repair/runtime-reliability-gateway`, targeting the same review base.
- The sole later activation target is the reviewed PR2 merge incorporating both slices. External operator owns lifecycle. No installed-runtime/config edit or restart was performed. Creative/pricing remain HELD.

## Implemented acceptance surface

- **S1:** native bootstrap/wrapper invocation-only save/restore (including absent and empty), exclusion from both export dumps; `_VAR_MAP` coverage; real child-process import path and delegation marker checks; concurrent contaminated readers; private atomic snapshot and native ordinary shell state preservation.
- **G1:** ordered gates execute afresh (including unchanged git status and already-dirty content), actual failure counters, one execution plus three default retries, preexhaustion, explicit-resume allowance reset, bounded output/timeout/error receipts, turn ceiling. Deprecated fingerprint field remains empty on serialization and is ignored on read. Judge/barrier limits and Kanban goal loop preserved.
- **F1:** one public SessionDB exact-raw multi-key CAS inside native `BEGIN IMMEDIATE`; independent connections and spawned processes, one absent-key winner, stale/tombstone rejection, rollback and controlled native lock contention. Goal CAS passes retry-patience `0.5`, not a total operation deadline.
- **F2:** every explicit mutation is conditionally persisted from fresh state, indexed edits conflict on changed controls, replacement has fresh UUID; read-only legacy state does not invent identity. Public loaded-state save captures exact raw/session/store provenance and cannot cross stores/sessions or overwrite a replacement. Two-key migration preserves status/budgets and invalidates old identity atomically.
- Evaluators claim before work and reserve one turn. Competing evaluators stay inert. Gates/judge results cannot overwrite a changed control. Delivery-only raw races reconcile persistence without rerunning work, preserving consumed tokens. Exhausted settlement conditionally releases/pauses the same owner or explicitly reports unknown retained claim. Crashed owner is never automatically stolen; explicit resume recovers it.
- Shared command parser reserves committed kickoff/resume fences; reservation conflict reports error with no prompt. Shared delivery helpers support pending validation and single-consumer continuation/notice claims.
- **Compatibility:** CLI/TUI command parity, two real post-turn/followup cycles without consuming gateway-only tokens, loaded-state save, positional GoalState constructor, session-control revision, migration/compression and gate authorization preserved.

## R1 independent-review correction

Konsult REQUEST_CHANGES on `a21cb844bc4547b7f9c7af7795ffd6bf893480bd` controls this loop; the earlier shell/gate/lane scoped approval is not approval of these new runtime/CI bytes. Native continuation claim: same card/worktree, run5382.

- Runtime commit `d0c43be686`: shared `is_waiting()` reloads state, parks retained owners, and conditionally expires only the exact unclaimed row without advancing control generation or clearing tokens. Every evaluator CAS attempt uses the snapshot that passed owner/status/wait eligibility; conflicts return to the same full eligibility check. Explicit resume/unwait remain controls. Nine native manager/real-CLI regressions went RED→GREEN. The unchanged `konsult_pr1_state_probes.py ... --expect-safe` now returns exit0 with all three predicates true. Broader canonical+preservation:28 files/404 passed. Reintroduced owner-loss/expiry-rebinding/ignored-wait mutations failed3/3/2 respectively, then were restored.
- Separate CI correction: validate original PR-event base/head objects, bounded missing-object fetch, reject unrelated/noncommit/malformed objects, use event `base..head` rather than checkout/main; non-PR calls perform no new attribution audit. Thirteen literal-step Git-fixture cases went RED→GREEN; hardcoded-range and missing-author-gate mutations fail2/1. Only `hello@kulltivate.ai → KulltivateAI` mapped via existing primitive, verified against GitHub's original commit API.
- Actual unassigned jobs documented by GitHub IDs101873731659(Python96),101873731100(JS32),101873731741(Rust32),101873731751(Windows32),101873732189(Nix32), all runner_id0; repository runner inventory0 and hosted-runner lookup404. No accessible matching assignment was found; no account/billing-cause claim. Standard-runner fallback is restricted to `KulltivateAI/hermes-agent`; upstream labels unchanged. Python full suite uses4 workers/90-minute finite bound, JS all checks uses concurrency2, Nix all outputs max-jobs2/cores2; Rust and native Windows retain host-sized/default bounded execution. Complete payloads, provisioning, permissions, callers and aggregate gates preserved by structural YAML comparison. No other runner jobs modified.
- Final local focused lane includes both regression files:22 files/307 passed. Actual hosted exact-head results are required and recorded in the subsequent PR handoff, not inferred from these edits. `ci-reviewed` remains reviewer-owned and absent pending new CI review. PR1/PR2/combined activation boundaries below are unchanged.

## Frozen shared contract for PR2

`GoalCommandResult.goal_fence` and evaluation `decision['goal_fence']`:

```text
{session_id: str, goal_id: UUID str, generation: int,
 continuation_id: UUID str | '', notice_id: UUID str | ''}
```

- Scope is the manager's captured profile store plus session key. An ID is opaque, not prompt text/time.
- `reserve_continuation() -> dict | None`: exact just-committed raw active row only, no reload/rebinding, generation increment or turn expenditure. Persistence errors raise `GoalPersistenceError`; CAS loss returns None.
- `continuation_pending(fence) -> bool`: read-only, fail-closed pre-enqueue check; not admission.
- `consume_continuation(fence) -> bool`: atomic matching-token clear preserving evaluator/unrelated delivery state. True is admission; False means stale/duplicate/malformed/inactive. Storage errors or exhausted contention raise `GoalPersistenceError`; do not admit on error.
- `consume_notice(fence) -> bool`: same claim immediately before actual send, including terminal-status notices. No retry on ambiguous network delivery.
- `GoalConflict` is a `GoalPersistenceError`/`RuntimeError` subclass. A superseded evaluation returns `verdict='stale'`, `should_continue=False`, no prompt and empty message. Interrupted/unknown persistence is explicit, never a successful control or continuation.
- Automatic evaluation does not require consumption of prior gateway delivery tokens: CLI/TUI transport is intentionally not generalized in this release.
- These APIs are synchronous. PR2 must use the existing ContextVar-preserving executor for DB work and wire the exact admission identity from real user and synthetic turns through post-turn evaluation. No DB lock spans tools/provider calls.

## Verification receipts

Provisioned separately, never in release venv:

```bash
uv sync --locked --python 3.11 --extra dev --extra messaging
.venv/bin/python -c 'import sys,pytest,pytest_asyncio; print(sys.executable,pytest.__version__)'
actionlint .github/workflows/runtime-reliability.yml
```

Local interpreter CPython 3.11.15; pytest 9.1.1; existing `uv.lock` unchanged. Its SQLite 3.50.4 uses the native safe DELETE-journal fallback; installed release SQLite is a separate adoption target. No new dependency/extras were needed beyond locked dev+messaging.

The **literal** final test step in `.github/workflows/runtime-reliability.yml` is the canonical acceptance command: `bash scripts/run_tests.sh -j 4` with its explicit PR1 file list. No future PR2 files, zero selections, mocked provider receipts or skipped checks count as acceptance.

- Pinned pristine baseline: 14 named existing preservation files, **242 passed / 0 failed**, exit 0.
- Initial desired-behavior RED: shell **3 failed** (legacy provenance leak); fresh gates **4 failed** (cached receipt, resume counters, preexhausted execution); CAS **2 failed** (missing primitive); generation fencing **11 failed** (stale saves/notices, competing owner, migration, missing reservation contract).
- Additional bounded-output RED: launch error returned 5031 characters rather than <=3000. Positional-constructor compatibility RED was caught and fixed during author review.
- Post-mutation canonical acceptance: **20 files, 285 passed / 0 failed**, exit 0. Exact-head hosted acceptance remains a mandatory PR handoff receipt, not inferred from local execution.
- `git diff --check`, focused Ruff and actionlint are author gates. Inherited required checks are never disabled.

Evidence directory on author/reviewer host: `/Users/kulltivate/code/runtime-reliability-evidence/` (candidate/post-mutation output, mutation diffs and assertions; final immutable head/CI checkpoint added at handoff).

### Mutation evidence (all restored before final acceptance)

Commands use canonical `bash scripts/run_tests.sh -j 4`:

- Omit restoration → `tests/tools/test_snapshot_invocation_authority.py`: **3 fail**, exit 1.
- Omit fixed-name dump exclusions → same file: **1 fail**, exit 1.
- Replay previous failed gate → `tests/hermes_cli/test_goal_gates.py -k unchanged_workspace_runs_fresh`: **1 fail**, exit 1 (only one actual receipt).
- Remove evaluator identity condition (allows stale evaluator to overwrite latest row) → `tests/hermes_cli/test_goal_generation_fencing.py -k control_during_judge`: **6 fail**, exit 1.
- Remove delivery token clear → same file `-k duplicate_consumer`: **1 fail**, exit 1.
- Delayed gateway-notice fence and effective-config mutations are **PR2-owned and not claimed here**.

## Remaining acceptance and next owner

Konsult independently reviews exact PR1 head and its real hosted lane, then explicitly merges into the pinned review base without activation. No author self-merge or coding delegates.

PR2 still owes the entire actual gateway producer/consumer map, admission snapshots, pending/overflow/orphan/startup/debounce/spool behavior, deferred-notice claims, effective Arch/Konsult true/true configuration and steering harness, preservation fixture, combined release manifest and externally operated installed-source acceptance/rollback. Merge, live adoption, whole-repair completion and any customer feature release are **not claimed by PR1**.

Routing/context: `/Users/kulltivate/.hermes/workspaces/kulltivate-ai/projects/2026-09-07-runtime-repair-active.md`.
