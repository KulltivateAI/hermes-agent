# Profile-scoped completion routing repair

Owner/author: Konsult. Base: approved runtime repair `07089268e308698ca0c8b63b28ebf35d03506910`. No installed-runtime change in this worktree.

## Demonstrated defect
Creative's real native multiplex fixture reaches process completion but not ordinary tool re-entry (`4 != 6` model fixture calls). `gateway/run_notifications.py` ignores `SessionSource.profile` when choosing the receiving adapter. Cold reconstruction also drops namespace and thread information. A named structured key can be mistaken for a raw API session ID.

## Bounded change
- Keep the global legacy parser unchanged. Add a completion-local namespace-aware route reader using the existing namespace extractor; only infer named-profile chat/thread IDs for unambiguous Discord key layouts. Other named platforms require explicit origin metadata; never treat Slack scope IDs as chat IDs.
- Preserve session-store origin, then cache, then reconstruction. Reject conflicting key/profile metadata instead of routing to another bot. Preserve recorded thread/user/scope and infer only safe missing thread fields.
- Compose existing `_adapter_for_source` ownership with existing primary/relay transport eligibility. Registered receiving-transport provenance remains authoritative; stale references grant nothing. No secondary lookup falls back to primary. Do not invent cold shared-credential transport provenance.
- Return retryable False for a valid named target whose transport is unavailable. Preserve completion claims: acknowledge only after acceptance, release on failure, retry without duplicate accepted output. Parent-session `/new` preflight is unchanged.
- Use the same local route reader for async delegation enrichment and prevent structured keys from entering raw API fallback.

## Proof / exit
New `tests/gateway/test_completion_profile_routing.py` covers stored/cached/cold/key-only routes, participant ambiguity, explicit Slack origin, unavailable secondary, provenance, primary relay/config eligibility, API isolation and acceptance-gated deduplication for process/delegation. Run via `scripts/run_tests.sh` using the existing dev interpreter through HERMES_PYTHON. Baseline: 20 failing cases, one passing case; no production correction yet at RED.
Run adjacent notification, relay priming, multiplex and goal-resume tests, the full approved runtime-reliability workflow, then Creative's complete native fixture against the resulting exact source. Preserve current standalone behavior and all native authorization/media/revision assertions. Require exact-head independent review and hosted CI before merge; external maintenance is still required for installation. This touches session/profile routing and must be explicitly risk-reviewed; it grants no new autonomy or deployment authority.

## Automation inventory / risk
The existing `.github/workflows/runtime-reliability.yml` remains pull-request-only on the dedicated repair base, `contents:read`, no stored checkout credentials, no merge/deploy/dispatcher action. Only its acceptance-file list is expanded to include the new routing cases and existing background-notification, relay-priming and multiplex-key suites. There is no new unattended activation vector. Source-profile/session routing and `.github` changes are explicitly high-risk review surfaces; the exact-head independent review must cover both. Installation remains externally maintained, never performed by the running gateway itself.

## Not claimed
No generic inverse session-key parser, raw text-only watcher migration, shutdown routing redesign, new supervisor, arbitrary cold shared-transport recovery, runtime self-install or Creative pixel/fleet acceptance from fixture results.
