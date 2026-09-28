# ADR-0007: Multi-harness strategy — one credential_process below every harness

Status: Accepted · Date: 2026-07-08

## Context

Enterprises run more than one coding harness against Bedrock (Claude Code CLI,
Claude Desktop/Cowork, and others). Each needs SSO auth, quota enforcement,
and per-user cost attribution. Building those three per harness does not
scale.

## Decision

Keep all governance in the AWS **`credential_process`** layer, below every
harness. The installer writes a dedicated AWS profile whose credentials come
from the credential-process binary
(`source/governed_inference_platform/cli/commands/package.py:3354-3412`; a
dedicated profile is used because a user's static `~/.aws/credentials` would
otherwise shadow it, `:3354-3356`). Any harness that can use an AWS named
profile inherits the full pipeline — OIDC/IDC auth, quota check at credential
issuance, STS session-name attribution — with **zero harness-side
integration**. Proven by the second harness already shipped: Claude Desktop
points `inferenceBedrockProfile` at that profile and "reuses the same
authentication pipeline as Claude Code CLI — no extra wrapper script to ship
or maintain" (`assets/docs/COWORK_3P.md:114`; `README.md:135-137`), and its
usage counts toward the same per-user quota
(`assets/docs/QUOTA_MONITORING.md:770-778`).

## Alternatives considered

- **Per-harness auth integrations.** Rejected: N bespoke integrations, each
  re-implementing token flows, quota calls, and attribution; every new harness
  starts at zero. The credential_process hook is the AWS-SDK-standard seam, so
  new harnesses start at done.
- **Mandatory gateway for all harnesses.** Rejected: the gateway supports only
  Claude Code v2.1.195+ as a client (`assets/docs/APPS_GATEWAY.md:42`) and
  loses per-user CUR/CloudTrail, IDC, and CI (see ADR-0003's table) — it
  cannot be the universal substrate.

## Consequences & optimizations

- **Telemetry gap:** harnesses that don't emit Claude Code's OTEL metrics are
  invisible to the client-telemetry quota pipeline. Covered by design:
  server-side metering from Bedrock invocation logs counts every invocation
  made with the issued credentials, regardless of harness (ADR-0005;
  `assets/docs/designs/server-side-metering-design.md` §1).
- Enforcement granularity for non-emitting harnesses is credential-issuance
  time only (see ADR-0009) until metering `max` mode lands.
- Extending harness-specific docs/config generation is queued as its own lane
  (branch `feat/ws-h2-multi-harness`, branched at `650d3d7`).

## Evidence

- `README.md:135-137`; `assets/docs/COWORK_3P.md:114`, `:169-179`.
- `package.py:3354-3412` (macOS/Linux), `:3786-3800` (Windows).
- `assets/docs/QUOTA_MONITORING.md:770-778`; ADR-0005 design doc.
