# ADR-0003: Claude apps gateway as an additional architecture, not a replacement

Status: Superseded in implementation by [ADR-0035](0035-mirror-upstream-claude-apps-gateway.md) · Date: 2026-07-07

## Context

The credential-process architecture has governance gaps recorded in
`REVIEW.md`: model restriction is client-side only (finding #1, `:23-32`),
Direct-STS attribution is client-asserted (finding #2, `:33-41`), and quota
enforcement trusts client telemetry (finding #3, `:42-48`). Anthropic's Claude
apps gateway solves those server-side — but gives up things enterprises also
need. Neither architecture dominates the other.

## Decision

Ship the gateway as an **additional** deployable stack
(`deployment/legacy/claude-apps-gateway.yaml`, commit `d6cae33`) and
document it as a complement with an explicit decision table
(`assets/docs/APPS_GATEWAY.md:14-38`; `README.md:108`: "The two can coexist in
one account"). The deciding dimensions (`APPS_GATEWAY.md:18-31`):

| Dimension | credential-process | gateway |
|---|---|---|
| Per-user CUR/CloudTrail attribution | per-developer STS principal (`:25`, `:27`) | single task role; per-user only via gateway telemetry (`:244-247`) |
| IAM Identity Center | supported | not supported, OIDC only (`:28`) |
| CI service tokens | headless credential_process (`:30`) | none — browser device flow only (`:239-240`) |
| Server-side model policy | none (IAM restricts by region; ADR-0006 adds opt-in scoping) | per-IdP-group allow-lists, 400 on violation (`:21`) |
| Settings delivery | baked into packages, rebuild to change | delivered at sign-in, hourly refresh, locked keys (`:22`) |

## Alternatives considered

- **Gateway-only (replace credential-process).** Rejected: loses per-user
  CUR/CloudTrail rows, IDC deployments, and CI pipelines outright (table rows
  above) — each is a hard requirement for a subset of existing adopters.
- **LiteLLM or another third-party LLM gateway.** Rejected: not Claude Code's
  control plane — no `forceLoginMethod`/`forceLoginGatewayUrl` client
  integration (`APPS_GATEWAY.md:176-197`) and no managed-settings delivery, so
  model locks would remain client-side, which is exactly finding #1; adds a
  non-Anthropic data-plane component to operate and patch.
- **Per-developer static API keys (Bedrock API keys / Mantle client-side).**
  Rejected: long-lived secrets contradict the short-lived STS design
  (`README.md:135-137`) and have no SSO offboarding or quota tie-in; Mantle is
  additionally not a supported gateway upstream (`APPS_GATEWAY.md:238`).

## Consequences & optimizations

- Two architectures to document and support; migration/coexistence guidance in
  `APPS_GATEWAY.md:33-38`.
- CLI parity added in lane E: `gip deploy/destroy/status gateway` and
  automatic `forceLogin*` keys in generated managed settings (`a2aa6e7`,
  `APPS_GATEWAY.md:193-197`).
- The gateway's inline spend caps become the offering for customers who want
  per-request enforcement — see ADR-0009.

## Evidence

- Commits: `d6cae33`, `a2aa6e7`. Files: `assets/docs/APPS_GATEWAY.md:14-38`,
  `README.md:104-137`, `REVIEW.md:16`, `:23-48`.
