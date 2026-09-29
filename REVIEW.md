# Enterprise Hardening Review — Findings and Changes

Deep review of this guidance against the requirements of a governed,
enterprise-ready Bedrock inference platform (per-user budgets, model
entitlements, data residency, scale). Branch based on `beta` at `d7b435f`.

This document started as the enterprise hardening review ledger and now records
which findings have been resolved in this branch, which are partially
addressed, and which remain follow-ups.

## What this branch changes

| Commit | Summary |
|---|---|
| `fix(quota)` | Cost-based limits ($ budgets, the wizard's recommended mode) now work end-to-end without fine-grained DynamoDB policies. Previously the wizard's cost answers never reached `deploy`, the template's `QuotaMode`/`MonthlyCostLimitUsd`/`DailyCostLimitUsd` parameters were wired to nothing, and the quota_check Lambda's env-default path required `MONTHLY_TOKEN_LIMIT > 0` (always 0 in cost mode → unlimited). Also: token prompts no longer run in cost mode and overwrite the zeroed limits; the monthly-enforcement answer is persisted (was always resetting to "block"); cost limits are first-class `QuotaPolicy` schema fields with export/import round-trip. |
| `feat` | Claude Apps Gateway and Desktop bootstrap now mirror the exact `aws-samples/anthropic-on-aws` subtrees with pinned provenance and tested PR-only updates. Local deploy implementations are retired; explicit destroy remains for legacy GIP stacks. |
| `docs` | Truth pass: WEB_SEARCH.md was a committed `git show` artifact; IDC "coming soon" contradiction; 8h vs 12h session lifetime; stale `.ccwb-config/` path; presigned-URL default (48h, not 7d); broken links/anchors; collector sizing. |

## Findings not changed in this branch (recommended follow-ups)

### Governance / security

1. **Model restriction is client-side only.** **RESOLVED** (`81982b2`, default-on `RestrictToAnthropicModels` with explicit opt-out; app-inference-profiles remain wide — opaque IDs).  The per-user IAM policy grants
   `bedrock:InvokeModel` on `foundation-model/*` and `inference-profile/*`
   restricted only by region (`bedrock-auth-okta.yaml:100-124` and siblings).
   A user with valid STS credentials can invoke *any* enabled model in the
   allowed regions — including non-Anthropic models — regardless of
   `managed-settings.json` model locks. Options: (a) optional
   template parameter narrowing resources to `foundation-model/anthropic.*` +
   `inference-profile/<prefix>.anthropic.*`, (b) recommend the upstream apps
   gateway where hard model entitlements are
   required.
2. **Cost-attribution identity is client-asserted.** **RESOLVED** (ADR-0015: opt-in `SessionNameBinding=email|sub` template parameter binds `sts:RoleSessionName` to the IdP-signed claim via a trust-policy condition; the earlier note that "cryptographic binding is impossible for generic OIDC" was wrong — IAM's Default OIDC mapping exposes `email`/`sub` as trust-policy condition keys for any IdP. Default `none` preserves prior behavior).  In Direct STS mode the
   client builds `RoleSessionName` from the JWT email
   (`source/go/internal/federation/sts.go:86-103`), but nothing binds it: the
   trust policy (`bedrock-auth-okta.yaml:166-174`) has no
   `sts:RoleSessionName` condition, so a modified client can attribute usage
   to any string. CloudTrail/CUR attribution is therefore spoofable by a
   malicious insider (quota enforcement is not affected for OIDC — the quota
   API identity comes from the validated JWT). Mitigation: add a trust-policy
   condition binding the session name, or use `sts:SourceIdentity`.
3. **Quota enforcement trusts client telemetry and is bounded by session
   lifetime.** **RESOLVED (Phase 1)** (`f555a1b`, server-side metering stack: shadow reconciliation + `METERING_MODE=max`; strict mode gated on cache-token fidelity). Usage is measured from client OTEL (sidecar can be stopped —
   bypass detection is detective-only, `QUOTA_MONITORING.md`), and blocking
   happens at credential issuance + periodic re-check, so a blocked user's
   live STS session (up to 12 h) keeps working. The docs' "Future
   Enhancements" already names the fix: meter server-side from Bedrock model
   invocation logs. This is the single highest-value governance improvement.
4. **Quota API responses always return HTTP 200 with CORS `*`** **RESOLVED** (`81982b2`, `CorsAllowedOrigins` parameter). 
   (`quota_check/index.py:build_response`). Low risk (JWT-authorized GET),
   but `Access-Control-Allow-Origin: *` on an authenticated API is worth
   tightening.
5. **AgentCore websearch gateway authorizes any valid id_token** **RESOLVED**
   (`d7161f9`, ADR-0014: opt-in `EntitledGroups` parameter attaches a
   default-deny Cedar policy engine to the gateway authorizer, `LOG_ONLY`
   mode first; residual: per-user metering of search spend remains future
   work — the tripwire is CloudWatch gateway invocation metrics). Original
   issue: the `CUSTOM_JWT` authorizer checked only `aud == client_id`
   (`bedrock-agentcore-gateway.yaml:119-127`), so every employee in the IdP
   app, including users with no Bedrock entitlement, can use (metered, $7/1k
   queries) search. There is also no per-user metering of search spend in the
   quota system.
6. **Presigned-S3 distribution stack uses a static IAM user access key** **RESOLVED** (`272ea6b`, `PresignPrincipalType=role` option; 12h URL cap trade-off documented). 
   (`RESOURCE_INVENTORY.md`, distribution stack) — an anti-pattern many
   enterprise SCPs block outright; role-based presigning would remove it.

### Cost-mode follow-ups (building on this branch's fix)

7. **quota_monitor SNS warnings are token-only.** **RESOLVED** (`df07bbf`).  The 80/90% warning
   alerts compare tokens against token thresholds
   (`quota_monitor/index.py:check_limits_and_generate_alerts`); in cost mode
   (limits now enforced by quota_check) users get no "approaching budget"
   warnings. Wire `MONTHLY_COST_LIMIT_USD` into the monitor's env-default
   policy and add cost-percentage alerts.
8. **`gip quota set` CLI still writes cost attrs via raw UpdateExpression** **RESOLVED** (`df07bbf`). 
   (`cli/commands/quota.py:177-211`). Works, but now that the dataclass
   carries the fields, routing through `QuotaPolicyManager.update_policy`
   removes the last side-channel write path.
9. **Analytics SQL hardcodes a flat $15/MTok** **RESOLVED** (`df07bbf`, exact per-family incl. cache rates). 
   (`analytics-pipeline.yaml:535,556,605`) — inconsistent with the per-family
   rates in `lambda-functions/shared/pricing.py`.

### Consistency / docs to verify with maintainers

10. **Sidecar + Cowork telemetry contradiction**: `init.py:983` ("Claude
    Desktop telemetry not supported" in sidecar mode) vs
    `QUOTA_MONITORING.md` / `COWORK_3P.md` ("both modes supported").
    **RESOLVED** — code-traced: `cli/utils/cowork_3p.py:add_monitoring_config`
    sets `otlpEndpoint = http://localhost:4318` in sidecar mode (via the
    `otel-helper --proxy` local forwarder, `otel_helper/__main__.py:run_proxy`),
    with unit coverage in `tests/cli/utils/test_cowork_3p.py`
    (`test_sidecar_mode_uses_local_proxy`). The `init.py` wizard string was the
    wrong side and has been fixed; docs kept. Residual note for maintainers:
    `deploy.py:452` still blocks the `cowork-dashboard` stack in sidecar mode
    with the message "Cowork cannot export telemetry in sidecar mode" — the
    *stack gate* may be legitimate (the dashboard's metric filters read log
    groups created by the central collector stack), but its message repeats the
    disproven telemetry claim and should be reworded when the gate is reviewed.
11. **`monthly_limit_millions` default drift**: **RESOLVED** —
    `_check_existing_deployment` now derives `monthly_limit_millions` from the
    saved `monthly_limit`; regression assertion added to
    `tests/cli/commands/test_init_quota_roundtrip.py`.
12. **`CHANGELOG.md` is a stub**: **RESOLVED** — replaced with a
    Keep-a-Changelog file summarizing this fork's changes and the
    v2.5.1/v2.5.2 beta history from git.

### Scale / enterprise operability

13. **Model catalog is hardcoded** **PARTIALLY ADDRESSED** (`c111a10`, `gip models check` drift detection; `f972ae5`/`17bd382`, lifecycle dates + EOL warnings, `--propose` codegen as the system of record, additive `extra_models` overlay, and a daily `model-lifecycle.yaml` alert ladder — ADR-0018; full dynamic discovery deliberately rejected as catalog forking).  (`models.py:_CLAUDE_MODELS_RAW`, ~1,150
    lines, plus two manually-synced preference lists at `models.py:1635` and
    `:1720`). Every model launch is a repo PR + client re-package. The live-API
    validator (`scripts/validate_bedrock_regions.py`) proves runtime
    discovery via `ListInferenceProfiles` is feasible.
14. **No non-interactive admin plane** **RESOLVED** (`4665024`, `gip init --from-file` + `--export-answers`). : `init` is a ~30-question wizard with
    no `--answers-file`; config changes require replaying it
    (`QUOTA_MONITORING.md`: "Re-run gip init and redeploy"). A GitOps-style
    `init --from-file` would unlock CI-managed deployments.
15. **No signed/MDM-native client packaging**: unsigned Go binaries trigger
    Windows Defender heuristics (documented workaround is a Defender
    exclusion, `TROUBLESHOOTING.md`); no `.pkg`/`.msi`, notarization, or
    auto-update channel.
16. **EU/residency deployment profile** **PARTIALLY ADDRESSED** (`272ea6b`, residency warnings in init/answers-file; full residency deployment mode still future work). : inference residency is well-guarded
    (`DATA_RESIDENCY_PREFIXES`, `models.py:1652`), but nothing warns when
    telemetry/quota/analytics stacks (user emails, usage records) land in a
    different geography than inference; websearch is us-east-1 only.
17. **Missing runbooks**: **RESOLVED** (RUNBOOKS.md §§1–8: IdP secret rotation
    across all three connection paths — confidential-client keyring, Cognito
    user-pool secrets, apps-gateway Secrets Manager `${ENV}` pattern; collector
    outage with honest quota-staleness impact plus a new fail-visible
    `HealthyHostCount` alarm in `otel-collector.yaml`; quota subsystem failure;
    upgrades; CoWork token rotation incl. bootstrap mode; user offboarding with
    a PII data-inventory table).  Original finding: IdP secret/cert rotation
    (Azure secrets expire),
    collector outage, quota Lambda failure, Cowork service-token rotation
    (static UUID, `init.py`), version upgrades. `TROUBLESHOOTING.md` is ~50
    lines.

## Verification

Last full run: 2026-08-15, committed `live-validation-fixes` branch.

- Python 3.12 active suite: 2982 passed, 32 skipped, 1 xpassed. Python 3.13 full
  suite: 3124 passed, 32 skipped, 1 xpassed, followed by focused validation of
  the final installer/config compatibility edits. CI now includes Python 3.13.
- `source/.venv/bin/ruff check .` and formatter check: clean.
- `mkdocs build --strict` in the dependency environment defined by
  `.github/workflows/docs.yml`: clean, with missing files, broken
  anchors, and unrecognized links escalated to build-failing warnings
  (`mkdocs.yml` validation block); CI runs the same command.
- A fresh Cognito OIDC sandbox deployed all 14 stacks from committed source;
  `gip test` passed 6/6 before and after the full LV matrix. Ubuntu, Windows
  Server 2022, and macOS clients authenticated and completed Claude Code
  inference. macOS cleanup removed every manifest-owned file and profile.
- Native Windows standard-user validation passed administrator refusal,
  read-only spaced-home install, injected rollback, idempotent reinstall,
  tamper refusal, foreign-profile refusal, and duplicate-section abort.
- `source/.venv/bin/python -c "import glob; from cfn_tools import load_yaml; [load_yaml(open(f).read()) for f in glob.glob('deployment/infrastructure/*.yaml')]"`:
  templates parse with CloudFormation intrinsic tags.
- `cfn-lint deployment/infrastructure/*.yaml`: warnings only; no errors. The
  warnings are existing packaging/runtime/deprecation warnings across multiple
  templates, including the expected local-code `W3002` warnings.
- `go test ./... -race` in `source/go`: clean.
- Mirrored upstream gateway tests: 17 CDK tests; Desktop bootstrap tests: 4 CDK
  and 5 Node tests; upstream shell contracts: 9 config-stamping and 19 setup
  helper tests. The mirror checksum/provenance check passes at the commit in
  `vendor/aws-samples/anthropic-on-aws/UPSTREAM.json`.
- Remaining validation gaps are explicit in `assets/docs/OPEN_ITEMS.md` and
  `assets/docs/LIVE_VALIDATION.md`: LV-1 payer-only billing readback,
  user-skipped Okta coverage, interactive macOS Keychain consent, and release
  of one EC2 Mac host that keeps LV-17 open until AWS's
  24-hour floor expires at 2026-08-16 12:50 UTC. Hardened cloud and local
  release backstops are active.
