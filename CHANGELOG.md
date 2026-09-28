# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
For releases older than those listed here, see
[GitHub Releases](https://github.com/aws-solutions-library-samples/guidance-for-claude-code-with-amazon-bedrock/releases)
(release notes are auto-generated from merged PR titles).

## [Unreleased]

Changes on this fork relative to upstream `beta`.

### Changed

- Claude Apps Gateway and Claude Desktop bootstrap now use a verified fetch of
  the exact `aws-samples/anthropic-on-aws` commit pinned in `UPSTREAM.json`.
  The upstream subtrees are not committed; the fetch reconstructs and checks
  their Git tree identities (paths, types, executable modes, symlink targets,
  and bytes). A weekly workflow tests pin updates and opens a review PR without
  auto-merging or deploying. Legacy profile fields and destroy commands remain
  only for migration and cleanup of previously deployed GIP stacks.
- Live validation on 2026-08-14 confirmed all 14 stacks deploy/update in a
  fresh Cognito OIDC sandbox in `us-east-1`; `gip test` passed all six checks,
  and Ubuntu 24.04, Windows Server 2022 standard-user, and macOS Sequoia arm64
  clients installed from presigned ZIPs, authenticated, and completed Claude
  Code inference. The Windows installer also passed the full native transaction
  matrix (admin refusal, rollback, reinstall, tamper and foreign-state refusal).
  Headless macOS Keychain re-read remains unverified because native consent UI
  is required.
- A second clean end-to-end run on 2026-08-15 redeployed all 14 stacks from
  committed source with no hand patches, passed `gip test` 6/6 before and after
  the full LV matrix, and repeated Linux, Windows, and macOS client coverage.
  macOS `gip cleanup` removed every manifest-owned file and AWS profile. The
  answers-file path now enables metering directly (F-031), removing the last
  manual profile edit from a non-interactive deployment.
- The supported Python range is now 3.10 through 3.13, and Linux distribution
  instructions explicitly require `unzip`.
- Guardrail defaults retain harmful-content filtering without
  `PROMPT_ATTACK`, use `Messages: COMPREHENSIVE` and `System: SELECTIVE` for
  Claude Code compatibility, roll immutable versions through
  `PolicyRevision`, and include apply-guardrail permissions.
- Memory is explicitly log-only, and web-search group entitlement is explicitly
  unavailable until their AgentCore identity representations are redesigned.
- Generated Aider and Pi configs use the selected model; live validation used
  Aider 0.86.2 and Pi 0.73.1 with Sonnet 4.5 because Sonnet 5 payloads were
  incompatible. OpenCode 1.18.18 inference and web-search MCP connected;
  Codex 0.147.0 received the expected governed Mantle deny.

- **Agent Registry GA cutover (ADR-0029 day-one execution, 2026-08-12).**
  The skills registry now defaults to the GA `agent-registry` API surface
  (`ga-2026-08-06`); `GIP_AGENT_REGISTRY_API_SURFACE=preview-2026-07-08`
  is the explicit fallback until the preview endpoints shut down
  2026-09-17. `update_skill_definition` is implemented on the GA surface
  (shape verified against the botocore `agent-registry-control/2025-12-01`
  service model, unblocking `gip skills approve` on GA), and the GA
  `UpdateRegistry` description wrapper is handled. `skills-registry.yaml`
  gains an `AgentRegistryApiSurface` parameter wired into both Lambda
  environments, dual-namespace IAM grants, and a dual-source
  pending-approval EventBridge rule (drop the `bedrock-agentcore` grants
  after 2026-09-17). Requires boto3/botocore >= 1.43.69 for the GA surface;
  CLI and quick-create packaging bundle boto3/botocore 1.43.70 into the
  skills Lambdas instead of relying on the runtime SDK.
- **Project renamed to Governed Inference Platform on Amazon Bedrock (`gip`).**
  The Python package is now `governed_inference_platform`, the Poetry project
  `governed-inference-platform`, the CLI `gip`, and the repository
  `guidance-for-governed-inference-platform-on-amazon-bedrock`.
- **BREAKING:** the rename is a clean break — no backwards-compat shims:
  - Admin config dir is now `~/.gip/` (was `~/.ccwb/`); end-user install dir
    `~/gip/` (was `~/claude-code-with-bedrock/`).
  - Keyring service is now `governed-inference-platform`
    (was `claude-code-with-bedrock`).
  - Env vars renamed to the `GIP_*` family (`GIP_PROFILE`,
    `GIP_CLIENT_SECRET`, `GIP_EVAL_*`, `GIP_MONITORING_TOKEN`); the Windows
    installer is `gip-install.ps1`.
  - Default AWS resource names changed (`gip-*` Lambdas/SNS/ECS/CloudTrail,
    `GIP*` CloudWatch namespaces, `/aws/gip/*` log groups, `/gip/*` SSM
    parameters, AWS profile `gip`).
  - Existing deployments keep working as-deployed; upgrading to this version
    requires redeploying the stacks and re-running `gip package` so clients
    pick up the new names. Product-contract identifiers (`claude_code.*`
    OTEL metrics, `CLAUDE_CODE_USE_BEDROCK`-family env vars read by Claude
    Code/Desktop) are unchanged.

### Added

- Claude apps gateway deployment on ECS Fargate (`d6cae33`), with full
  `gip deploy/destroy/status gateway` CLI wiring and automatic
  `forceLoginMethod`/`forceLoginGatewayUrl` managed-settings injection
  (`a2aa6e7`).
- Server-side tamper-proof usage metering, Phase 1: per-region
  `quota-metering.yaml` stack sources per-user usage from Bedrock model
  invocation logs (metadata only), reconciles against client telemetry with a
  drift metric + SNS alert, and offers `METERING_MODE=max` enforcement
  (`f555a1b`; design in `assets/docs/designs/server-side-metering-design.md`).
- Prompt-cache savings visibility: `calculate_cache_savings()` helper,
  dashboard widgets (org cache hit rate, estimated $ saved, per-user cache
  read/write), and exact per-family Athena queries `CacheSavingsByUser` /
  `CacheSavingsByModel` (`c52f60e`).
- Multi-harness support: `gip package --harnesses` generates verified Bedrock
  configs for OpenCode, OpenAI Codex CLI, Pi, and Aider on top of the same
  credential-process SSO/quota/attribution plug; flagship doc
  `assets/docs/HARNESSES.md` (`f70b1b4`).
- Default-on server-side model governance: `RestrictToAnthropicModels` parameter with explicit opt-out for approved exceptions
  across all auth stacks scopes IAM invoke permissions to Anthropic models
  (`81982b2`).
- Non-interactive/GitOps init: `gip init --from-file answers.yaml` with
  `--export-answers` round-trip (`4665024`).
- Cost-mode SNS budget warnings at 80/90/100% of monthly/daily USD budgets
  (`df07bbf`).
- `gip models check` — live model-catalog drift detection against
  `ListFoundationModels`/`ListInferenceProfiles` (`c111a10`).
- Data-residency warnings in the init review step and answers-file mode, plus
  a role-based presigning option (`PresignPrincipalType=role`) for the
  distribution stack that eliminates the static IAM user access key
  (`272ea6b`).
- Architecture Decision Records 0001–0010 (`assets/docs/adr/`) documenting
  decisions, alternatives considered, and evidence (`25767fc`); Wave 3 added
  ADRs 0011–0024 covering every accepted, rejected, and deferred decision.
- Operational runbooks (`assets/docs/RUNBOOKS.md`): IdP client-secret/certificate
  rotation, OTEL collector outage, quota subsystem failure, version upgrades,
  and Cowork service-token rotation.
- `REVIEW.md` — enterprise hardening findings and follow-ups (`8c9f8c2`).
- Per-user gateway entitlements: opt-in Cedar policy engine on the AgentCore
  gateway authorizer — a valid id_token alone no longer grants tool access
  (REVIEW #5; `d7161f9`; ADR-0014).
- Opt-in `sts:RoleSessionName` trust-policy binding (`SessionNameBinding=email|sub`)
  so CUR/CloudTrail attribution is IdP-signed, not client-asserted
  (`4e36b2f`, `dcd720c`; ADR-0015).
- Structural parity test suite for the six OIDC auth templates, plus the
  google-template drift fix it caught (`8f66976`, `cf47fb4`).
- Opt-in account-level Bedrock Guardrails enforcement stack
  (`guardrails-enforcement.yaml`; `055bedb`; ADR-0020).
- Opt-in AgentCore Memory stack behind the websearch gateway: extracted-only
  default, customer-managed KMS, retention sweeper, per-user erasure
  (`gip memory forget-user`), data-classification doc `MEMORY.md`
  (`9fc74f8`, `55066b4`; ADR-0016).
- Governed skills registry: `skills-registry.yaml` (S3 artifacts + Agent
  Registry) with `gip skills publish/list/approve/sync` and
  `assets/docs/SKILLS_REGISTRY.md` (`f1de421`, `164629c`; ADR-0017), plus a
  deterministic skill eval runner `gip skills eval` driven by `eval.yaml`
  with drift-vs-baseline scoring (ADR-0023).
- Model lifecycle automation: lifecycle dates and EOL warnings in
  `gip models check`, `--propose` codegen, additive `extra_models` overlay,
  and a daily `model-lifecycle.yaml` alert ladder (`f972ae5`, `17bd382`;
  ADR-0018).
- Gateway MCP tools in every capable harness: Claude Code via native
  `headersHelper` config, OpenCode/Codex via a new
  `credential-process --mcp-proxy` stdio shim with per-request token refresh
  (`1201660`, `a700d6b`; ADR-0024).
- Mantle endpoint governance: explicit `DenyBedrockMantleEndpoint` on
  governed credentials until metering parity exists (`d818b2b`), plus a
  fail-visible `MantleEndpointUsageAlarm` tripwire in each metering stack
  (ADR-0022).
- Optional Application Inference Profile attribution: per-tier AIP ARNs from
  `gip init` override Claude Code model defaults at packaging for
  team/cost-center cost allocation (ADR-0021).
- Enterprise deployment guides: `MULTI_ACCOUNT.md`, `NETWORK_ISOLATION.md`,
  `SIEM_EXPORT.md`, the IdP requirements contract (`8d554b1`, `7676fca`,
  `436e65e`, `34ff970`; ADR-0019), and a per-component failure matrix
  `FAILURE_POSTURE.md`.

### Deprecated

- Python credential provider (`source/credential_provider/`): formally marked
  deprecated in favor of the Go credential-process (default since June 2026).
  Still shipped via `gip package --legacy` and as the automatic fallback when
  Go >= 1.24 is unavailable; also anchors the Go/Python parity tests. No
  functional change. Removal conditions and timeline:
  `assets/docs/adr/0013-legacy-python-credential-provider.md`.

### Fixed

- The landing-page distribution backend verifies the ALB-signed identity header
  before trusting it: the token must use `ES256`, must carry this deployment's
  load balancer ARN in its `signer` field, and must not be expired, and its
  segments are decoded as base64url rather than standard base64. The ALB invoke
  permission is scoped to the stack's own target group instead of every target
  group in the account, which requires a one-time target-group replacement when
  upgrading a landing-page deployment (`assets/docs/RUNBOOKS.md`).
- Quota enforcement for IAM Identity Center users resolves the caller from the
  HTTP API payload format 2.0 IAM context (`requestContext.authorizer.iam`),
  keeping the payload format 1.0 location as a fallback. IDC quota checks
  previously found no identity on the deployed route and failed closed with
  `missing_identity`. The Identity Center setup guide now states when the
  developer bundle includes credential-process and when quota is enforced.
- Stable releases are cut only from `main`. A manual workflow dispatch from any
  other ref no longer tags or publishes a release, binaries are published only
  when the release job succeeds, and both jobs check out and verify the released
  commit so published binaries match the tagged tree.
- Live-validation environment and prerequisite fixes: Python 3.13 is included
  in the supported range (F-001), and Linux installation guidance includes
  `unzip` commands for supported distributions (F-015).
- Guardrail discovery and teardown inspect enabled, configured, or explicitly
  requested regions rather than every partition region (F-002), and destroy
  removes cross-region artifact stacks created by metering (F-030).
- CloudFormation templates larger than the 51,200-byte inline limit are uploaded
  to the deployment artifacts bucket and submitted by `TemplateURL` (F-003).
- Cost-mode quota deployment and default policies retain cost accounting
  (F-004); cost-only `gip quota set-*` commands no longer require a token limit
  (F-013); and fine-grained policy mutations fail clearly when fine-grained
  enforcement is disabled (F-014).
- Metering, log-group, and lifecycle resource tags use only service-supported
  characters (F-005).
- Guardrails no longer configure unsupported prompt-attack output filtering
  (F-006), federated roles can apply enforced guardrails (F-011), Claude Code
  defaults exclude prompt-attack scanning and selectively scan system content
  (F-016), and `PolicyRevision` rolls policy changes into a new immutable
  enforced version (F-019).
- Skills Registry dependency bundling falls back to `uv pip` when the active
  Python environment has no `pip` module (F-007), and registry readiness polling
  accommodates the observed asynchronous creation time (F-009).
- AgentCore Memory remains explicitly log-only and no longer attempts to forward
  the restricted `Authorization` header pending an identity-context redesign
  (F-008).
- Packaged `config.json` uses the canonical nested `profiles` schema while the
  Python credential provider and package tests retain legacy flat-schema
  compatibility (F-010).
- `gip test` resolves credential-process paths before changing its subprocess
  working directory (F-012).
- Windows installation resolves placeholders in staged destination files without
  mutating the source package and fails fast without interactive pauses (F-017),
  uses a non-ambiguous harness staging variable (F-026), and explicitly returns
  success after a completed headless install (F-027).
- The macOS installer displays Keychain consent guidance only when a packaged
  profile selects keyring storage (F-018).
- Installers place resolved harness and MCP configs under `~/gip`, preserving
  nested paths and replacing credential-helper placeholders (F-020), and
  distributed archives include the complete generated `harnesses/` tree
  (F-021).
- Generated Aider and Pi configs use the selected compatible model instead of an
  incompatible Sonnet 5 default (F-022, F-025).
- The web-search gateway role includes policy-engine read and decision actions
  required by AgentCore's creation checks (F-023). Group enforcement remains
  unavailable because AgentCore exposes Cognito group claims to Cedar as strings
  rather than sets, pending delimiter-safe identity redesign (F-024).
- Live-validation procedures use a token fixture that reaches the intended
  internet-facing shared-token rule (F-028), and document the explicit
  `RestrictToAnthropicModels=false` governance decision required to test opaque
  application inference profiles (F-029).

- Collector ingress now rejects plaintext by default. Production and
  internet-facing central monitoring require HTTPS; an explicit internal-only
  local-development compatibility flag is documented for temporary migration.
- Customer-facing regional guidance now routes verified Windows CodeBuild
  workloads in London and Canada Central in-region, warns that opt-in AgentCore
  Memory content is stored in `us-east-1`, and removes the analytics partition
  projection's 2030 hard stop.
- Windows installer transactions now preserve file-backed content under Windows
  PowerShell 5.1 and clean up only empty directories created by a failed run.
- Documentation now keeps enforcement state inside each inference cell, lists
  every production gateway gate, and estimates infrastructure with explicit
  formulas that include the Claude apps gateway.
- quota: cost-based limits now work end-to-end without fine-grained policies
  (`9d38075`).
- quota: DynamoDB failures now propagate so `fail_closed` actually denies —
  previously swallowed and treated as "allow" (`8316324`).
- metering: dedup markers commit atomically with usage accrual via
  `TransactWriteItems`, closing a double-count window on retries (`c694ab8`);
  cache-token capture broadened to every documented spelling family, counted
  once per read/write class.
- IAM hardening from the CFN security review: otel-collector port-80 listener
  now redirects to HTTPS, end-user `PutMetricData` grants namespace-scoped,
  `SecretStoreRole` scoped to its one secret, analytics confused-deputy trust
  conditions, CodeBuild logs policy scoped (`934e591`, `051c75c`, `96f4c23`,
  `8983f8b`, `23ddfe4`).
- init: the all-commercial region sentinel expands into real regions at
  assignment (`90d8e58`); `gip deploy s3bucket` is dispatchable (`7d6124d`);
  GovCloud model-tier resolution respects the us-gov residency guard
  (`3aa9df8`); `okta_auth_server` is packaged under both runtime keys
  (`5138662`).
- bootstrap: device-code grant expiry is enforced in the request path
  (`ce5c419`).
- init wizard: the monthly token-limit prompt default no longer reverts to 225
  on re-run — `_check_existing_deployment` now derives `monthly_limit_millions`
  from the saved raw-token limit.
- init wizard: the sidecar monitoring mode description incorrectly stated that
  Claude Desktop telemetry is not supported; sidecar mode is supported via the
  local otel-helper proxy (`otel-helper --proxy`).

### Documentation

- Replaced high-level cost guidance with a cited scenario model covering cached
  and uncached usage, topology-dependent infrastructure floors, optional
  component add-ons, CRIS pricing ambiguity, and runtime-dependent exclusions.
- Added an account-local inference-cell architecture and an evidence-dated
  eight-region deployability matrix covering CRIS residency, regional model
  subsets, optional AgentCore content-service location, and validation limits.
- Docs truth pass — fixed stale and contradictory documentation (`036ac09`).
- Replaced the CHANGELOG stub with this Keep-a-Changelog file.

### Security hardening

- Go binaries spawn child processes through one audited site,
  `internal/proc`, built on `os.StartProcess`: `$BROWSER` launches and the
  otel-helper's call to its sibling `credential-process` now require an
  absolute executable path (a `$BROWSER` value is resolved with `LookPath`
  first, so PATH lookups keep working; a relative path can no longer resolve
  against the working directory). Timeout-kill semantics for the
  credential-process call are unchanged.
- The localhost quota-status page and the OIDC callback page are rendered
  with `html/template` instead of string formatting plus manual escaping;
  the markup is unchanged and the quota API message is still escaped.
- Removed `source/tests/docker-compose.test.yml` (an unreferenced Dec-2025
  scratch environment that piped `curl` into `bash`); CONTRIBUTING.md carries
  an equivalent `docker run` one-liner.
- Tests: the confidential-client thumbprint test asserts the `x5t#S256`
  header directly (the provider never emits SHA-1 `x5t`); test subprocess
  helpers delegate to the CLI's audited `cli.utils.proc` choke point (or
  mirror its form where the package is not importable); IdP/HTTP stubs emit
  JSON via `encoding/json`. No behavior change.
- **CloudFormation (`deployment/infrastructure/`): every scan change is an
  in-place stack update** — no logical ID is renamed or removed, no resource is
  replaced, and no new parameter is required. The integrated tree adds
  `QuotaAlertTopicPolicy` (quota-monitoring) and `LoggingBucketPolicy`
  (codebuild-windows, distribution, presigned-s3-distribution), while retaining
  MAIN's key contracts: `QuotaAlertTopicKey`/`QuotaAlertTopicKeyAlias`,
  `LifecycleAlertTopicKey`/`LifecycleAlertTopicKeyAlias`, and the unconditional
  `CuratorTopicKey`/`CuratorTopicKeyAlias` shared by the skills topic and DLQ.
  There is no `SkillsRegistryKey` resource. Deployers of the quota,
  model-lifecycle and skills stacks need KMS key administration (at least
  `kms:CreateKey`, `kms:CreateAlias`, `kms:PutKeyPolicy`,
  `kms:EnableKeyRotation`; `kms:ScheduleKeyDeletion` to delete the stack) and
  `sns:SetTopicAttributes` for the topic policies; see
  `assets/docs/RESOURCE_INVENTORY.md`.
- **Alert topics are encrypted with a stack-owned KMS key** (`gip-quota-alerts`,
  `gip-model-lifecycle-alerts`, `<stack>-pending-approval`). The quota and
  model-lifecycle keys are conditional on creating their topics; the skills
  stack always creates exactly one `CuratorTopicKey` because its distributor
  DLQ also uses it, and the topic reuses that key when created. The AWS-managed
  `aws/sns` key cannot be used because its key policy cannot grant CloudWatch
  alarms or EventBridge. Cost: USD 1/month per created customer-managed key,
  rising to USD 3/month after two annual rotations; `gip deploy` with quota,
  model-lifecycle and skills creates two (quota + skills; model-lifecycle reuses
  the quota topic). Enabling memory without an external key adds one. Delivery
  fixes shipped with it: the quota topic gains
  the topic policy it never had (CloudWatch alarms and, for the model-lifecycle
  Health rule that `gip deploy` points at this topic, EventBridge), and the
  model-lifecycle topic policy also admits its error alarm. The lifecycle
  Lambda is granted key use through SNS for a reused encrypted topic.
  **UPDATE window:** CloudFormation may re-encrypt a topic seconds before the
  publishing Lambda role receives its key grant (the role references the topic,
  so the order cannot be forced without a cycle); when model-lifecycle reuses
  the quota topic, the window spans the two stack updates. One missed
  alert at most; alarms re-evaluate on their period.
- **Dead-letter queues use `KmsMasterKeyId`** instead of `SqsManagedSseEnabled`:
  the AWS-managed `aws/sqs` key on the five possible Lambda-only DLQs (no IAM
  change, no key cost; KMS request charges are expected to stay inside the
  20k/month free tier for normally-empty queues but are not guaranteed zero), and the
  skills stack key on `DistributorDeadLetterQueue`, whose second writer is
  EventBridge. Same UPDATE window as above for the distributor role's grant
  (seconds); a Lambda failure whose DLQ write lands in it is reported as
  `DeadLetterErrors` and re-rendered by the next 15-minute run.
- **CodeBuild artifacts** are written with SSE-KMS under the AWS-managed
  `aws/s3` key (`EncryptionKey` set explicitly; `EncryptionDisabled` removed)
  instead of the build bucket's SSE-S3 default. Same-account readers
  (`gip package`, `gip distribute`, CI) are covered by that key's policy via
  S3; no IAM change. **Unverified live:** one build + one download after the
  update.
- Quota DynamoDB tables name `alias/aws/dynamodb` in `SSESpecification` (the
  key `SSEType: KMS` already selected; no change to data or cost).
- **Server-access-log delivery repaired** for the codebuild, distribution and
  presigned-s3-distribution log buckets: they had no bucket policy, so with
  S3 Object Ownership enforced (the default since April 2023) log delivery
  was silently dropped. Each gains a `logging.s3.amazonaws.com` `s3:PutObject`
  grant scoped by `aws:SourceAccount` + `aws:SourceArn` and a TLS-only deny.
  The existing analytics, skills-registry, CloudFormation-artifact and landing-
  page log policies now also bind `aws:SourceArn` to their actual source
  buckets, preventing another same-account bucket from injecting log objects.
  **UPDATE caveat:** CloudFormation `PutBucketPolicy` replaces any policy a
  customer attached to these log buckets by hand.
- **The upstream Claude Apps Gateway subtrees are no longer tracked**
  (ADR-0036). `vendor/aws-samples/anthropic-on-aws/{claude-apps-gateway,
  claude-apps-gateway-bootstrap}` (65 files) are removed from the repository
  and gitignored; `UPSTREAM.json`, `LICENSE`, and `README.md` remain committed.
  `UPSTREAM.json` keeps the pin (repository, commit, tree ids) and records the
  versioned `git-tree-sha1-v1` verification format.
  `scripts/fetch-claude-apps-gateway.sh` materializes the pinned commit with
  plain `git`, reconstructs Git trees from every path/type/executable
  mode/symlink target/file byte, and removes what it wrote on any mismatch
  (`--verify`, `--clean`, `--print-tree-id`). Unsupported filesystem types are
  rejected, and verification uses only isolated temporary Git state.
  `scripts/sync-claude-apps-gateway.sh` now only proposes a pin (rewrites
  `UPSTREAM.json` and the `LICENSE` copy); the weekly workflow tests the
  materialized pin and opens a PR that changes the pin only and links the
  upstream diff. **Operator change:** run `scripts/fetch-claude-apps-gateway.sh`
  before `cd vendor/aws-samples/anthropic-on-aws/claude-apps-gateway` (needs
  `git`, `jq`, github.com). Rollback: check out the previous `UPSTREAM.json`,
  `--clean`, fetch. The committed mirror had drifted from the recorded pin
  (two example JWT secrets rewritten by the round-2 secrets pass); the fetch
  restores upstream's exact bytes.

## [2.5.2-beta] — 2026-07-03 to 2026-07-07

Summarized from git history `v2.5.1-beta.27..v2.5.2-beta.8`.

### Added

- package: `--prepare-offline` flag for air-gapped builds (#753).
- Declarative `extra_files` for package + distribute (#744).

### Fixed

- quota: aggregate token usage with `sum_over_time`, not `increase`
  (delta metric) (#754).
- quota: price cache-write (`cacheCreation`) tokens in the cost estimate (#756).
- quota: cost-based enforcement reads the correct DynamoDB attributes (#748).
- otel: resolve OIDC provider type before `--get-monitoring-token` so silent
  bearer refresh works (#749).
- init: users can set `max_session_duration` and it is retained on re-run (#745).

## [2.5.1-beta] — 2026-07-01 to 2026-07-03

Summarized from git history `v2.5.0-beta.145..v2.5.1-beta.27`
(v2.5.1 released 2026-07-03).

### Added

- `ccwb doctor` — one-command troubleshooting (#704).
- Web search: AgentCore web search gateway wired into `ccwb deploy`/`destroy`
  (#641), injected into the Cowork MDM config (#647), delivered via the
  bootstrap config response (#708), with an init-wizard opt-in (#717).
- Claude Desktop dynamic config delivery + organization plugin distribution
  (#706).
- models: Claude Sonnet 5 support (#729); EU CRIS profiles for Opus 4.8 and
  Opus 4.7 (#734, #738).
- dashboard: per-user cost/token panels on the Cowork dashboard (#740);
  Avg Cost Per Turn panel (#741).

### Fixed

- cowork: generate Bedrock bearer token for `inferenceCredentialHelper` (#733).
- deploy: pass profile tags to CloudFormation stacks (#730).
- idc: recover IDC sign-in in-session instead of failing fast (#731).

### Documentation

- WEB_SEARCH.md usage and deployment guide (#644); AgentCore Gateway web
  search for Claude Cowork (#648).
- Per-stack AWS resource inventory for SCP planning (#416).
- Proxy mode marked experimental; bootstrap recommended for Claude Desktop
  telemetry (#736); clarified Claude Desktop supports native `otlpHeaders`
  (#659); demoted the claude-bedrock launcher to optional (#732).
