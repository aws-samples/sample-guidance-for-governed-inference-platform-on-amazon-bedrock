# ADR-0005: Server-side metering source = Bedrock model invocation logs

Status: Accepted · Phase 1 implemented (`quota-metering.yaml`); cache-token fidelity verified 2026-08-14 · Date: 2026-07-07

## Context

Quota usage is measured today from client OTEL: a developer who stops the
sidecar becomes invisible to quotas; detection is detective-only
(`REVIEW.md:42-48` finding #3; `assets/docs/QUOTA_MONITORING.md:700-744`
"Sidecar Bypass Detection"). The docs' own Future Enhancements names
server-side metering as the fix (`QUOTA_MONITORING.md:813-817`). A design was
produced in lane F (`750808f`, merged `650d3d7`):
`assets/docs/designs/server-side-metering-design.md`.

## Decision

Meter from **Bedrock model invocation logging** as the sole accrual source:
per-region log group → subscription filter → processor Lambda → additive
`server_*` attributes on the existing `UserQuotaMetrics` items; enforcement
becomes `max(client, server)` (design §3). Rollout is phased via a
`METERING_MODE` env on quota_check: `shadow` (collect + reconcile only, the
default) → `max` → `strict` (design §4 item 4, §8 "Phased rollout").

## Alternatives considered

- **CloudTrail as the metering source.** Rejected: CloudTrail management
  events for `InvokeModel` carry **no token counts** — the documented example
  entry has `responseElements: null` (design §2 table, citation [2]). It
  cannot meter; it keeps its detective role and gains a coverage check (§7.5).
- **Inline proxying of Bedrock calls.** Rejected as an explicit non-goal
  (design §1): putting a proxy in the data plane makes its latency and
  availability part of every inference call — an availability blast radius the
  credential-process architecture exists to avoid (see ADR-0009).
- **Client OTEL only (status quo).** Rejected: zero tamper resistance —
  "stop sidecar, edit exporter" (design §2 table).

## Consequences & optimizations

- One stack per allowed Bedrock region; CRIS logs stay in the *source* region
  so destination regions are irrelevant to collection (design §5).
- Prompt capture stays OFF: all five data-delivery booleans false; metering
  reads metadata only (~$40/mo per 1,000 developers, design §6).
- Backwards compatible by construction: absent `server_*` attributes read as
  0 and `max()` degenerates to client figures (design §8).
- **Cache-token fidelity is resolved-positive.** In a live `us-east-1` run on
  2026-08-14, metadata-only invocation logs exactly matched the Converse usage
  blocks for cache write (7,613 tokens), cache read (7,613 tokens), input, and
  output counts. This satisfies the cache-field prerequisite identified by Q1.
  Session-name binding (`REVIEW.md:33-41` finding #2) remains a separate
  prerequisite for trusted per-user strict enforcement (design §8 Phase 3).

## Evidence

- `assets/docs/designs/server-side-metering-design.md` (§1-§9), commit
  `750808f` / merge `650d3d7`.
- `REVIEW.md:42-48`, `QUOTA_MONITORING.md:700-744`, `:813-817`.
- Live validation LV-5, Cognito OIDC sandbox, `us-east-1`, 2026-08-14.
