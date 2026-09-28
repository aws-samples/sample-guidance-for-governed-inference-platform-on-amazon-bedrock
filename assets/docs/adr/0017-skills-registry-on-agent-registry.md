# 0017 — Governed skills registry on AWS Agent Registry

Status: Accepted; Desktop delivery portion superseded by [ADR-0035](0035-mirror-upstream-claude-apps-gateway.md) · 2026-07-09
Research: an internal research memo, an internal research memo

## Context

Organizations need one governed, account-common catalog of agent skills with
an approval workflow, consumed by five harnesses (Claude Code, CoWork,
OpenCode, Codex, AgentCore harness). The repo already documented — but never
implemented — a `gip plugins add/sync` feeder for the now-retired local CoWork
bootstrap `/plugins` endpoint. AWS Agent Registry (AgentCore,
preview since 2026-04-09) supports "Agent Skills" as a first-class record type
with Draft → Pending → Approved lifecycle, IAM personas, EventBridge and
CloudTrail — but it is a metadata catalog: skill files cannot be stored in it,
there is no CloudFormation type, and the service migrates namespaces
(`bedrock-agentcore` → `agent-registry`) starting 2026-08-06 with old
endpoints shut down 2026-09-17 (registry-faq, retrieved 2026-07-08).

## Decision

Compose, don't build: **Agent Registry = governance source of truth; S3 =
artifact store (`skills/<name>/<version>/`, immutable versions, sha256-pinned
zips); distributor Lambda = dumb renderer** of APPROVED records into each
harness's native channel (`marketplace.json` for Claude Code and
`skills-lock.json` for `gip skills sync`). A dormant `plugins-registry.json`
compatibility output remains only for legacy raw-CloudFormation consumers;
new Desktop delivery follows ADR-0035. The registry is provisioned by a Lambda custom resource
(`Custom::AgentRegistry`, adopt-if-exists, retain-on-delete). Publisher and
curator are IAM roles optionally gated on IdP groups via
`aws:PrincipalTag/groups`. Record metadata carries a frozen `_meta` schema
(`io.gip.skill/v1`) with reserved eval/fork hooks so the eval lane (E-S3)
builds against a stable contract. `gip skills publish/list/approve/sync`
supersedes the unimplemented `gip plugins` interface. Cross-account ships as
an optional `aws:PrincipalOrgID` bucket-read policy only.

## Rejected: pure-S3 greenfield registry (owner direction)

A self-built S3+DynamoDB registry would avoid the preview-namespace risk, but
was rejected by owner direction: it re-implements approval state, audit,
personas, and discovery that Agent Registry provides as a managed service, and
it forfeits the ecosystem trajectory (registry MCP endpoint, JWT
group-gated discovery, first-class IaC types announced for GA). Build-scope
discipline: we own renderers and validation, not a governance database.

## Risk: namespace migration (accepted, mitigated)

The top risk is deliberate: the preview API we call is scheduled to break
(2026-08-06 new namespace + schema; 2026-09-17 old endpoints die). Mitigation
baked into the design: (a) every registry API call is isolated in one client
module per runtime (`skills_registry/registry_client.py`,
`cli/utils/agent_registry.py`) with the API surface pinned and dated in the
module header; (b) registry state is re-creatable — content lives in git + S3,
the registry owns only approval state, so post-migration recovery is a
re-publish + re-approve replay; (c) the migration week is budgeted as an
explicit wave task, not discovered when endpoints 4xx. Deferred until GA:
JWT/MCP discovery registry, registry-side org sharing, KMS artifact signing
(schema hook present), model→version resolver (E-S3/R15).

---

**Amendment (2026-07-29):** The budgeted migration task ran 8 days before the
2026-08-06 break. AWS published the full GA migration guide
(https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-faq.html,
retrieved 2026-07-29): new `agent-registry` namespace, unchanged operation
names, breaking record/registry schema changes, and required data migration.
Both pinned client modules now carry dual dated surfaces
(`preview-2026-07-08` default, `ga-2026-08-06`) behind one switch
(`GIP_AGENT_REGISTRY_API_SURFACE`), record readers accept both descriptor
shapes, and the preview surface fails visibly at the published break dates.
The GA `UpdateRegistryRecord` request shape is not yet documented and is
fail-loud until verified at GA. Mitigation (b) — replay from git+S3 — was
verified and documented. Cutover procedure, knowns/unknowns, and the day-one
runbook: ADR-0029 (`0029-agent-registry-api-migration.md`). Note:
`skills-registry.yaml` IAM/EventBridge/env updates are day-one work at GA.
