# ADR-0011: Reposition as a governed inference platform, not a Claude Code guide

Status: Accepted; superseded in part by [ADR-0030](0030-gip-rename-clean-break.md) (2026-07-29: the Wave 6 rebrand reversed the "identifiers unchanged" decision below; the platform-first positioning stands) · Date: 2026-07-08

## Context

The guidance began as "deploy Claude Code on Bedrock." By this fork's second
hardening wave, the same infrastructure demonstrably serves more than one
harness: Claude Code and Claude Desktop (upstream scope), plus OpenCode,
Codex CLI, Pi, and Aider via the same `credential_process` plug
(`assets/docs/HARNESSES.md`, commit `f70b1b4`), with two connection
architectures (per-developer STS federation and the Claude apps gateway,
`assets/docs/APPS_GATEWAY.md`). Governance — SSO, USD/token budgets, cost
attribution, model allow-lists, server-side metering — is enforced below the
harness layer (IAM/STS/quota-gated issuance/invocation logs), so the harness
is genuinely pluggable. A title scoped to one harness undersells the
platform to the reader evaluating it and hides the multi-harness capability
in a subsection.

## Decision

Retitle the entry points to platform-first framing: README H1 becomes
"Guidance for a Governed AI Inference Platform on Amazon Bedrock" with a
harness table up front and a dedicated "Connecting Harnesses" section
(Claude Code, Claude Desktop/Cowork, other harnesses); mkdocs `site_name`
and the docs-index intro follow. The **repository slug, package names, CLI
name (`ccwb`), stack names, and upstream badge links are unchanged** — the
rename is editorial positioning, not an identifier migration.

## Alternatives considered

1. **Keep the Claude Code title, keep harnesses as a subsection** — rejected:
   misrepresents scope; the strongest differentiator (one governed endpoint,
   any harness) was buried at heading level 3.
2. **Rename identifiers too (repo slug, `ccwb`, stack prefixes)** — rejected:
   breaks upstream diffability, existing deployments, published links, and
   the GitHub Releases binary pipeline for near-zero reader benefit.
3. **Split into a separate "platform" repo wrapping this one** — rejected for
   now: doubles maintenance surface; upstream may adopt the reframing
   directly (this ADR is part of the proposal to maintainers).

## Consequences & optimizations

- Readers evaluate the platform first and pick harness sections second,
  matching how `DEPLOYMENT_PATHS.md` routes customer profiles.
- Upstream PR framing: title change is isolated to README/mkdocs/docs-index
  so maintainers can accept or reject it independently of code changes.
- Claude Code remains the flagship, first-listed harness — the reframing
  adds scope; it does not demote the original use case.

## Evidence

- Multi-harness capability: `assets/docs/HARNESSES.md`, `f70b1b4`.
- Two architectures: `assets/docs/APPS_GATEWAY.md`, `d6cae33`/`a2aa6e7`.
- Below-harness governance: `81982b2` (IAM model scoping), `f555a1b`
  (invocation-log metering), quota gate at credential issuance
  (`deployment/infrastructure/lambda-functions/quota_check/index.py`).
