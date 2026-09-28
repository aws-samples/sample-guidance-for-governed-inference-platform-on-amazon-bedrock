# ADR-0035: Mirror the upstream Claude Apps Gateway without local forks

Status: Accepted; delivery mechanism (committed mirror, sync-into-tree) superseded by [ADR-0036](0036-verified-fetch-claude-apps-gateway.md) on 2026-09-02 — the decision intent (no local fork, upstream authoritative, pinned commit) stands · Date: 2026-08-15

## Context

GIP implemented its own CloudFormation gateway and two Claude Desktop bootstrap
services. The AWS Samples `anthropic-on-aws` repository now carries a complete
Gateway CDK application, the gateway-native Desktop `/user/bootstrap`
contract, and a companion PKCE configuration/MCP add-on. Their topology,
outputs, health behavior, routing defaults, and client contract differ from the
local implementations. Translating between them would create permanent drift.

## Decision

- Mirror `claude-apps-gateway/` and `claude-apps-gateway-bootstrap/`
  byte-for-byte under `vendor/aws-samples/anthropic-on-aws/`.
- Record the repository commit and subtree hashes in `UPSTREAM.json`.
- Refresh through `scripts/sync-claude-apps-gateway.sh`; a weekly workflow runs
  upstream tests and opens a review PR. Never auto-merge or auto-deploy.
- Retire `gip deploy gateway` and `gip deploy bootstrap`. Keep explicit destroy
  support only for legacy GIP stacks.
- Keep legacy profile fields loadable for migration, but never translate them
  into upstream CDK settings.
- Treat upstream docs, tests, resource topology, Desktop contract, and runbooks
  as authoritative. GIP documentation may link or summarize provenance only.

## Consequences

New deployments cannot drift through GIP-specific gateway patches. Upstream
changes remain reviewable and reproducible. Existing deployments require a
side-by-side migration; no safe in-place CloudFormation-to-CDK conversion exists
for the stateful PostgreSQL-backed gateway.

Supersedes the implementation portions of ADR-0003, ADR-0004, ADR-0025,
ADR-0032, and ADR-0033.
