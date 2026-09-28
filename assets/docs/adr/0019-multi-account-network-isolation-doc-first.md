# 0019 — Multi-account and network isolation ship as documented options, doc-first

Status: Accepted · 2026-07-08 · Wave 3 lane D2
Research: an internal research memo, an internal research memo

## Context

Large customers ask for (a) one inference account per Geo/Country/LoB under a
central payer with AWS Organizations (owner requirement, 2026-07-08) and (b)
private-network access via PrivateLink. Research established that both are
mostly *placement and wiring* questions, not re-architectures:

- Bedrock core has **no resource-based policies** (foundation models /
  inference profiles) — inference always executes, is logged, and is billed in
  the account whose role the user assumed, so the auth stack is inseparable
  from the inference account (R3 §1; Bedrock IAM reference, read 2026-07-08).
  The current single-account deployment dropped into a dedicated member account
  already matches the AWS SRA "Generative AI account" pattern.
- Every AWS-side network path can be private today except the quota HTTP API
  (no private endpoint type) and the OIDC IdP exchange (public SaaS); the
  server-side stacks already carry the needed hooks (`ALBScheme=internal`,
  gateway ALB hard-coded internal) (R2 §1-2).

## Decision

Ship **documentation, not code**, this wave:

- `MULTI_ACCOUNT.md` — Topology A (single GenAI member account, default) and
  Topology B (hub-and-spoke: auth+metering per inference account via StackSets,
  collector/quota/analytics central), the cross-account capability matrix, the
  Geo/LoB residency pattern, the SCP library, and payer-account cost rollup.
- `NETWORK_ISOLATION.md` — decision tree, VPC-endpoint inventory, the
  `execute-api` private-DNS 403 hazard, residual public surface, an inline
  example endpoint template snippet, and the opt-in `aws:SourceVpce` deny as
  documented JSON.

**Deferred code items** (tracked as known limitations, from R3 §6): B1
quota-table ARN hardcodes `${AWS::AccountId}`; B2 metering processor addresses
the table by name not ARN; B3 `gip deploy` is single-account by design; B4 one
package per account; B5 no org-read artifacts-bucket policy for StackSet
assets; B6 collector/analytics same-account coupling. Also deferred:
a `RequireVpce` template parameter and a managed `vpce-endpoints.yaml` stack
(R2 §1), and a private-subnet `networking.yaml` variant.

## Alternatives considered

- **Replumb metering onto Kinesis/Firehose logical destinations** to
  centralize the quota ledger cross-account — rejected: a re-architecture of a
  working pipeline for a wiring problem two small code items (B1/B2) solve
  later (R3 §3).
- **Ship `vpce-endpoints.yaml` as a managed stack in `deploy.py`** — rejected
  this wave: isolation-minded enterprises bring their own VPC/endpoint
  management; a doc snippet serves them without adding a stack lifecycle (R2 §1).
- **Parameterize the `aws:SourceVpce` deny now** — rejected: hard-blocks any
  off-VPN developer; must stay opt-in documented JSON until demanded (A7).

## Consequences

- Topology B is deployable today by console/StackSets with per-account quota
  ledgers; a single central ledger waits on B1/B2.
- The quota API remains public-with-JWT even in isolated deployments; the
  documented hazard (private-DNS `execute-api` endpoint breaks it with 403)
  is the operative risk to communicate.
- Bearer-token-through-VPCE was live-verified in `us-east-1` on 2026-08-14;
  CloudTrail recorded `callWithBearerToken=true` and the matching endpoint ID.

## Evidence

`assets/docs/MULTI_ACCOUNT.md`, `assets/docs/NETWORK_ISOLATION.md` (this
lane); `deployment/infrastructure/quota-metering.yaml:95-120,226-263`;
`deployment/infrastructure/quota-monitoring.yaml:481-486`;
`source/governed_inference_platform/cli/commands/deploy.py:726-735,957-996`;
dated URLs inline in both docs and in R2/R3.
