# ADR-0012: Wave 3 operating decisions

Status: Accepted · Date: 2026-07-08

## Context

Wave 3 is a full-depth audit and hardening pass across the platform: pain-gap
analysis, implementation elegance versus official AWS guidance, provider
pluggability, AgentCore capability build-out (web search rollout, user and
organizational memory), and `bedrock-mantle` endpoint governance. It runs as
parallel worktree lanes orchestrated by a main agent with sub-agents.
Four operating questions had no obvious answer; each was debated by an
independent sparring agent producing steelman/attack/flip-conditions per option.
This ADR records the outcomes. Full memos live in the wave ledger
(the internal wave specification, the internal delivery ledger, the internal project operating axioms).

## Decision 1 — Live AWS experiments: hybrid, orchestrator-only credentials

Static validation is provably blind where Wave 3 is novel: AgentCore CFN resource
types carry cfn-lint suppressions (`bedrock-agentcore-gateway.yaml` E3002) because
schemas lag, and failure-posture tests are definitionally live. Full per-lane
deploys fail on default VPC/EIP quotas (~4 concurrent lanes/account) and would put
credentials in ~50 autonomous agents. Therefore: static-only by default; an
allowlist of lanes needing live proof (gateway authz, memory, inference-profile
attribution, Mantle parity, chaos) deploys sequentially into one sandbox
(a single development sandbox account, us-east-1, `w3-*` prefixes, `wave3=active` tags, $150 tag-filtered
budget, nightly sweep), with only the orchestrator holding credentials.
Async-signal experiments (CUR lags 8–24h; memory extraction is async) deploy on
day 1. Rejected: per-lane live deploys (quota wall, blast radius, ~10× cost for
mostly redundant signal); static-only (merges never-deployed templates, defers
failures to post-merge bisection).

## Decision 2 — Upstream diffability binds structural refactors; drift is caught by parity tests

The six `bedrock-auth-*.yaml` templates stay six files. Consolidation into one
conditioned template is rejected: the per-provider OIDC-provider logical IDs mean
consolidation breaks every existing customer stack on update (CFN creates before
deletes; `AWS::IAM::OIDCProvider` URLs are unique per account → `EntityAlreadyExists`
rollback), provider-specific parameter validation degrades, and the deployment layer
permanently forks from upstream. A generator with byte-stable rendering is rejected
because achieving byte stability requires reformatting all templates — destroying
diffability to protect it. Instead, a structural parity test
(`test_auth_template_parity.py`) canonicalizes all templates and deep-compares
resource skeletons, with an exception list that asserts expected per-provider values
so exceptions cannot hide new drift. Evidence this is needed and sufficient: sparring
found live upstream-inherited drift in HEAD (`bedrock-auth-google.yaml` missing
`bedrock:CallWithBearerToken`, `ListFoundationModels`, `ListInferenceProfiles`, and
`application-inference-profile/*` ARNs; `cloudwatch:namespace` condition present in
only 2 of 7 templates), while the one feature with a parameterized cross-template
test (`RestrictToAnthropicModels`) shows perfect parity. Flip condition: exception
list exceeding ~15 entries signals genuine divergence; revisit then.

## Decision 3 — Push per merged lane, forward-only

No `.gitlab-ci.yml` exists (pushes trigger nothing); all lane merges are no-ff merge
commits so reverts are `git revert -m 1` without force-push; worktrees share one
local object store, making the laptop a single point of failure during multi-day
unattended runs. Therefore: push `enterprise-hardening` + the lane branch after
every gated merge; tag `ckpt/wave3-baseline`, `ckpt/wave3-batchN`, and
`ckpt/wave3-complete`; never `--force`; history rewrites are prohibited (curation
for upstream happens in fresh PR branches).

## Decision 4 — Memory ships opt-in with graduated modes; the telemetry axiom gains a second clause

AgentCore Memory stores conversation-derived content, which the metadata-only
telemetry posture (ADR-0005 lineage) does not cover. Verified facts shaping the
decision: raw-event retention floor is 3 days (`eventExpiryDuration` minimum;
CreateMemory API), `DeleteEvent` permits earlier deletion, extraction is async with
completion events streamable to Kinesis, and erasure is enumerate-and-delete
(`ListActors`/`BatchDeleteMemoryRecords` — no single purge API). Decision: memory is
a separate opt-in stack, default mode `extracted-only` (raw events retained at most
3 days and deleted after extraction; only extracted facts/preferences/summaries
persist), org memory (`/org/`, curated writes) and user memory (`/users/{actorId}/`)
independently enableable, KMS-CMK encrypted, region-pinnable, with
`gip memory forget-user` and an offboarding runbook shipping in the same wave.
Two hard gates: the gateway per-user authorization fix (REVIEW #5) lands first
(actorId must derive from the validated JWT, never a tool argument), and erasure
tooling ships with the feature, not after. The axiom is restated in two clauses
(an internal delivery record A2/A3): telemetry stays metadata-only unconditionally;
content-bearing features are opt-in, isolated, and erasable. Rejected: shipping
memory as an unsupported example (publishes ungoverned content storage while
claiming a clean core); org-memory-only (concedes the 1P-parity point without
reducing the DPIA work).

## Decision 5 — Mantle governance parity is an axiom, not a feature

`bedrock-mantle.{region}.api.aws` is a second inference front door (OpenAI Responses/
Chat Completions + Anthropic Messages APIs, own quota set, Projects/Workspaces
attribution). The platform's metering reads bedrock-runtime invocation logs and its
bypass detection joins CloudTrail against OTEL; whether Mantle traffic is visible to
either is unverified. Axiom A4: no endpoint reachable by governed credentials may be
invisible to metering — Wave 3 either extends metering to Mantle or scopes IAM to
fail closed until it can, decided by live evidence, not assumption.

E-A4 chose the fail-closed branch: every governed Bedrock credential policy now
includes an explicit `Deny` on `bedrock-mantle:*` (`DenyBedrockMantleEndpoint`).
This remains in place until Mantle has invocation logging / attribution parity or
another metering source that satisfies A4.

## Consequences

- Sub-agents never hold AWS credentials; live proof is centralized and sequential.
- The `w3-*`/`wave3=active` namespace is the only mutable surface in the sandbox.
- Every lane's accept/reject decision produces or updates an ADR (axiom A6).
- A dedicated demo account remains a follow-up (internal account-provisioning prerequisite;
  see an internal delivery record); isolation is contractual (A9) rather than account-level
  for this wave.
