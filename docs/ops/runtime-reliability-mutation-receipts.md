# Runtime PR2 correction / mutation receipts

Author execution, based on reviewed candidate `3dbc11fadb4df70a92c2ffa1404c1ba68cf0b063`.
These are verification receipts, not independent approval or installed acceptance.
All mutations were temporary. Original source bytes were restored and hash-checked
before the green run. No live gateway, provider, profile, process kill or DB mutation.

Run each selection with:

```bash
bash scripts/run_tests.sh -j 2 --file-retries 0 <test-file> -- -k <selection> --tb=short
```

## Mutations and observed results

1. **Delayed notice send fence** — `gateway/run_goals.py`, replace the
   `if not claimed: return` guard with `if False: return`.
   File `tests/gateway/test_goal_event_fencing.py`, selection
   `command_producer_and_delayed_notice`: mutant **2 failed / exit 1** (stale notice
   send called once); restored **2 passed / exit 0**.
2. **Effective Discord configuration wiring** — `agent/agent_init.py`, replace
   `_agent_cfg.get("tool_loop_guardrails", {}), platform=platform` with
   `{}, platform=platform` at actual controller construction.
   File `tests/gateway/test_runtime_repair_effective_config.py`, selection `normal`:
   mutant **1 failed, 2 passed / exit 1** (`agent_received_discord_config` false);
   restored **3 passed / exit 0**.
   Initial mutation survived because the negative profile verdict masked disconnected
   runtime wiring. Added unconditional real-agent equality assertions; reran the
   SAME mutation to the expected failure. Do not count the initial survival as proof.
3. **Review parent-resource preservation** — `agent/background_review.py`, replace
   `review_agent.release_clients()` with `review_agent.close()` in
   `_release_fork_clients`.
   File `tests/run_agent/test_background_review.py`, selection
   `native_review_worker_preserves`: mutant **2 failed / exit 1** (parent process
   cleanup invoked), separately for normal success and ordinary worker crash;
   restored **2 passed / exit 0**. The native worker and real release/close methods
   run; only destructive physical cleanup and provider work are substituted.
4. **Generated goal control policy** — `gateway/run_turn.py`, remove the
   `pending_event is None or pending_event.allow_gateway_control` predicate from
   the pending slash safety net.
   File `tests/gateway/test_goal_adapter_fencing.py`, selection
   `generated_slash_goal`: mutant **2 failed / exit 1**, missing `/stop` and
   `/status` events; restored **2 passed / exit 0**, through native admission.
   Separate actual/eventless command protection tests also pass.
5. **Cold offline bootstrap** — `scripts/runtime_reliability_acceptance.py`, remove
   the `tools.tirith_security.ensure_installed` fixture substitution.
   File `tests/gateway/test_runtime_repair_effective_config.py`, selection `cold`:
   mutant **3 failed / exit 1**, network attempt at
   `tirith_security._background_install -> _install_tirith -> _download_file`;
   restored **3 passed / exit 0**, zero attempts. This reproduces the original
   Ubuntu focused CI failure locally by forcing the binary-lookup miss, not by
   faking the host OS. No security config/defaults are changed.

## Combined restoration

Executed the actual final `run` string parsed from
`.github/workflows/runtime-reliability.yml`, not a manually copied subset:

```text
=== Summary: 43 files, 546 tests passed, 0 failed (100% complete) in 22.1s (4 workers) ===
exit 0
```

Ruff on changed Python files, Windows-footgun diff scan, and `git diff --check`
pass. Workflow contents are unchanged from `3dbc11f`; selected files gained tests.
Hosted checks must run on the final corrected SHA; this receipt does not waive them.

Raw author evidence retained under
`/Users/kulltivate/code/runtime-reliability-evidence/`:
`pr2-r2-mutation-receipts.json`, `pr2-r2-<mutation-name>.log`,
`pr2-r2-initial-config-mutation-survived.log`, and
`pr2-r2-combined-acceptance.log`.
