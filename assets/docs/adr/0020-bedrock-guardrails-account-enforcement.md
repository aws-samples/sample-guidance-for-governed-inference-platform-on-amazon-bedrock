# ADR-0020: Bedrock Guardrails Via Account-Level Enforcement

## Status

Accepted.

## Context

Wave 3 R1 evaluated three guardrail paths for the governed inference platform: IAM `bedrock:GuardrailIdentifier`, standalone `ApplyGuardrail`, and account-level enforced guardrail configuration (an internal research memo). The platform supports multiple harnesses, including clients that cannot attach arbitrary Bedrock guardrail headers.

## Decision

Ship an opt-in `guardrails` stack that creates a Bedrock guardrail, pins an
immutable version, and configures
`AWS::Bedrock::EnforcedGuardrailConfiguration` in every configured Bedrock
region. The default content policy blocks harmful-content categories at
`MEDIUM` strength without adding `PROMPT_ATTACK`, uses
`Messages: COMPREHENSIVE` and `System: SELECTIVE` for Claude Code
compatibility, and leaves trace disabled. `PolicyRevision` forces immutable
version rollout when policy behavior changes, and governed roles include
`bedrock:ApplyGuardrail`.

## Alternatives Considered

- IAM `bedrock:GuardrailIdentifier` enforcement: rejected as the primary mechanism because calls without guardrail headers are denied, and supported harnesses cannot reliably attach those headers.
- `ApplyGuardrail`: rejected because this repository deliberately avoids an inline inference proxy and does not own the Claude apps gateway request pipeline.
- Organization-level Bedrock policy: deferred to multi-account/SCP rollout guidance; the first implementation is per-account and region-scoped.

## Consequences

- Guardrails are opt-in and fail closed if the enforced configuration is invalid.
- Enforcement is account-and-region scoped; dedicated inference accounts are the recommended boundary.
- Guardrail metrics are metadata-only, but guardrail evaluation itself processes prompt/response content transiently.
- Mantle remains explicitly denied until its governance and metering parity are proven.
