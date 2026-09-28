# Architecture Decision Records

Rationale for the major decisions of this fork's enterprise-hardening work
(branches `feat/ws-a..f` merged into `enterprise-hardening`). Format is
MADR-ish: Context / Decision / Alternatives considered / Consequences /
Evidence. Every factual claim cites a file (`path:line`) or commit (short SHA)
in this repository.

| ADR | Title | Status | Summary |
|---|---|---|---|
| [0001](0001-base-on-upstream-beta.md) | Base on upstream `beta`, not `main` | Accepted | `main` is release-only; `beta` carried 3 cost-quota fixes we build on. |
| [0002](0002-cost-limits-first-class-schema-fields.md) | Cost limits as first-class QuotaPolicy schema fields | Accepted | Raw DDB side-attributes caused upstream #748 and a dead env-default path. |
| [0003](0003-apps-gateway-additional-architecture.md) | Claude apps gateway as an additional architecture | Superseded in implementation by 0035 | The architectural choice remains; GIP no longer owns its deployment implementation. |
| [0004](0004-gateway-config-cfn-rendered-env.md) | Gateway config via CFN-rendered env var + `${ENV}` secret placeholders | Superseded by 0035 | Local config rendering was replaced by the exact upstream CDK. |
| [0005](0005-server-side-metering-from-invocation-logs.md) | Server-side metering from Bedrock model invocation logs | Accepted; cache fidelity verified | CloudTrail has no token counts; metadata-only cache write/read and input/output counts matched live on 2026-08-14. |
| [0006](0006-default-on-iam-model-scoping.md) | Server-side model governance via default-on IAM resource scoping | Accepted | Auth stacks now scope to Anthropic models by default; explicit opt-out remains for approved exceptions. |
| [0007](0007-multi-harness-credential-process.md) | Multi-harness strategy: one credential_process below every harness | Accepted | Harnesses inherit SSO+quota+attribution with zero integration work. |
| [0008](0008-non-interactive-init-answers-file.md) | Non-interactive init via answers file mirroring the wizard dict | Accepted | Zero drift with the wizard; export round-trip as migration path. |
| [0009](0009-enforcement-at-credential-issuance.md) | Enforcement stays at credential issuance + re-check | Accepted | Inline blocking needs a data-plane proxy; gateway offers that for those who want it. |
| [0011](0011-platform-first-positioning.md) | Reposition as a governed inference platform, not a Claude Code guide | Accepted; superseded in part by 0030 | Editorial retitle only — no identifier/slug changes (identifier portion reversed by the Wave 6 rename, [0030](0030-gip-rename-clean-break.md)). |
| [0012](0012-wave3-operating-decisions.md) | Wave 3 operating decisions: hybrid live experiments, diffability + parity tests, per-lane push, memory data axiom, Mantle parity axiom | Accepted | Five debated decisions with rejected alternatives; introduced the project operating axioms. |
| [0013](0013-legacy-python-credential-provider.md) | Legacy Python credential provider: deprecate in place, do not remove | Proposed | Still shipped (`--legacy` + Go-missing fallback), upstream-active, parity-test anchor; removal only as future upstream proposal. |
| [0014](0014-gateway-cedar-entitlements.md) | Gateway per-user entitlement via Cedar Policy engine | Accepted; current design rejected | Cognito group claims surface as strings, so the set-based policy cannot be created; non-empty groups and ENFORCE are rejected pending redesign. |
| [0015](0015-session-name-binding.md) | Opt-in `sts:RoleSessionName` trust-policy binding for Direct STS | Accepted | Binds CUR session-name attribution to the IdP-signed email/sub claim; SourceIdentity rejected (not in CUR, no AssumeRoleWithWebIdentity parameter). |
| [0016](0016-agentcore-memory-stack.md) | AgentCore Memory stack: server-derived actorId behind a deploy gate | Accepted; log-only | AgentCore rejects `Authorization` in allowed request headers; active identity-dependent tools are rejected pending redesign. |
| [0017](0017-skills-registry-on-agent-registry.md) | Governed skills registry on AWS Agent Registry | Accepted; Desktop delivery superseded by 0035 | Registry = governance, S3 = artifacts, distributor = dumb renderer; Desktop uses the mirrored upstream delivery contract. |
| [0018](0018-model-lifecycle-overlay-vs-codegen.md) | Model discovery: `--propose` codegen as system of record, additive `extra_models` overlay | Accepted | Full runtime overlay catalog rejected (forks the catalog); overlay is additive-only and excluded from tier chains; wildcard IAM default kept, `PinnedModelAllowlist` deferred with design recorded. |
| [0019](0019-multi-account-network-isolation-doc-first.md) | Multi-account + network isolation as documented options, doc-first | Superseded | [0026](0026-account-local-inference-cell-state.md) replaces its multi-account placement; the network-isolation decision is retained. |
| [0020](0020-bedrock-guardrails-account-enforcement.md) | Bedrock Guardrails via account-level enforcement | Accepted | IAM guardrail-header enforcement bricks unsupported harnesses; opt-in regional account-level enforcement covers every Bedrock runtime path without client changes. |
| [0021](0021-application-inference-profiles-additive.md) | Application Inference Profiles as optional team/cost-center attribution (additive) | Accepted | AIP ARNs from init override Claude Code tier defaults at packaging; per-user AIPs rejected (quota-infeasible, AWS-discouraged); other harnesses excluded pending live verification; ABAC enforcement deferred opt-in. |
| [0022](0022-mantle-metering-deferred-tripwire.md) | Mantle per-user metering deferred; explicit deny + account-level tripwire | Accepted; alarm verified | No identity source exists; deny stays. The tripwire moved `OK` to `ALARM` to `OK` in a live 2026-08-14 run. |
| [0023](0023-deterministic-skill-eval-runner.md) | Deterministic skill eval runner (local, hard assertions, no judge gating) | Accepted | Bedrock/AgentCore Evaluations rejected as substrate (can't execute skills/assert artifacts); `eval.yaml` + `gip skills eval` with fixture workspaces, drift-vs-baseline scoring; live model calls opt-in only; auto-fork and judge gating deferred. |
| [0024](0024-harness-mcp-wiring.md) | Gateway MCP for every harness: native `headersHelper` + Go stdio proxy | Accepted | Claude Code wired config-only; OpenCode/Codex via `--mcp-proxy` stdio shim (fresh token per request); `mcp-remote` and static-header entries rejected (1 h token expiry, browser OAuth, Node dependency); Pi/Aider skipped honestly. |
| [0025](0025-verified-telemetry-oidc-bearer.md) | Accept the validated OIDC bearer on verified telemetry ingress | Superseded by 0035 | The retired local bootstrap no longer owns gateway/Desktop telemetry identity. |
| [0026](0026-account-local-inference-cell-state.md) | Keep enforcement state inside each inference cell | Accepted; supersedes 0019 in part | Quota, metering, gateway state, and identifiable telemetry remain cell-local; only approved aggregates centralize; Wave 4B automation remains pending. |
| [0028](0028-discard-quota-lambda-sizing.md) | Discard quota Lambda sizing without equivalent authenticated traffic | Accepted | The live IAM baseline returned only `missing_identity`; ARM variants were not run and 128 MB x86_64 remains unchanged. |
| [0029](0029-agent-registry-api-migration.md) | Agent Registry preview→GA migration: dual pinned API surfaces, env-switched cutover | Accepted | Executes ADR-0017's budgeted migration; GA `UpdateRegistryRecord` shape fail-loud until verified at GA; day-one runbook included. |
| [0030](0030-gip-rename-clean-break.md) | Wave 6 rebrand: clean-break rename `ccwb`→`gip`, no compat shims | Accepted (retroactive) | Supersedes 0011's identifier-stability decision and A1's no-rename clause; accepts broken prebuilt-binary path until new-slug releases, orphaned deployments, reduced diffability; Anthropic product contracts allowlisted. |
| [0031](0031-defer-live-aws-validation.md) | Defer live AWS validation (V1a–V6) and ledger the gap | Accepted (retroactive) | Owner decision 2026-07-13; all runtime claims rest on local gates until the W7-LIVE backlog executes. |
| [0032](0032-defer-gateway-fail-open-spend-enforcement.md) | Gateway spend-enforcement posture during a Postgres outage (AUD-005) | Superseded by 0035 | Upstream owns gateway health and spend-failure behavior. |
| [0033](0033-defer-cowork-plugin-zip-delivery.md) | Keep Claude Desktop plugin ZIP delivery non-default until authenticated | Superseded by 0035 | Desktop plugin delivery follows the exact upstream contract. |
| [0034](0034-single-attempt-quota-fail-closed.md) | Keep quota checks single-attempt and fail-closed | Accepted | Retry experiments leaked workers/sockets in the legacy provider; alarms and explicit break-glass mode mitigate transient denial until a cancellable transport exists. |
| [0035](0035-mirror-upstream-claude-apps-gateway.md) | Mirror upstream Claude Apps Gateway without local forks | Accepted; delivery mechanism superseded by 0036 | Exact gateway/Desktop subtrees, pinned provenance, tested PR-only sync, legacy destroy-only migration. |
| [0036](0036-verified-fetch-claude-apps-gateway.md) | Deliver the pinned Claude Apps Gateway source by verified fetch, not a committed mirror | Accepted; supersedes 0035's mechanism | Subtrees untracked; versioned Git-tree reconstruction verifies path, type, executable mode, symlink target, and bytes and fails closed. Reason: security findings on third-party Dockerfiles/CDK cannot be suppressed without forking upstream. |

ADR-0029 is reserved by the in-flight Agent Registry migration lane (W7-REG).

## Conventions

- **Status** is one of Proposed / Accepted / Superseded. Supersession must
  link both directions (do not edit an accepted ADR's decision in place).
- New ADRs take the next number, added to this table.
- Keep each ADR ≤ 60 lines; evidence over prose.
