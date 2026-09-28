# ADR-0009: Enforcement stays at credential issuance + re-check, not inline per-request

Status: Accepted · Date: 2026-07-08

## Context

Quota enforcement happens when the credential-process exchanges tokens for AWS
credentials — browser auth and every silent refresh
(`assets/docs/QUOTA_MONITORING.md:656-668` "Enforcement Timing"). A blocked
user's live STS session keeps working until expiry, so the enforcement gap is
bounded by `max_session_duration` (configurable since `ff270be`, #745). The
question, sharpened by REVIEW finding #3, was whether hardening should move
enforcement inline into the request path.

## Decision

Keep enforcement at **credential issuance + periodic re-check**, and extend it
with server-side metering's `max` mode (server-measured usage feeds the same
issuance-time check; ADR-0005, design §4). Inline per-request blocking is an
explicit non-goal of the metering design
(`assets/docs/designs/server-side-metering-design.md:25-28`: "Enforcement
remains at credential issuance and periodic re-check … that gap is bounded by
`max_session_duration`, not by this design").

## Alternatives considered

- **Inline per-request blocking (data-plane proxy).** Rejected: clients call
  `bedrock-runtime` directly with SigV4 today; blocking mid-session requires
  interposing a proxy on every inference call, making its availability and
  latency part of every request. The solution's own outage analysis shows the
  value of the current split: during a collector outage "inference is
  unaffected — credentials and Bedrock access do not pass through the
  collector" (`assets/docs/RUNBOOKS.md`, §2 Symptoms). A proxy forfeits that
  property and the zero-server-footprint deployment mode
  (`assets/docs/APPS_GATEWAY.md:29`).
- **Shorter sessions as pseudo-inline enforcement.** Not adopted as policy,
  but available: admins can trade refresh overhead for a tighter gap via
  `max_session_duration` (`ff270be`).

## Consequences & optimizations

- The residual gap is explicit and documented
  (`QUOTA_MONITORING.md:768`: "Enforcement only at credential issuance").
- Customers who *do* want inline enforcement have a supported path without
  this solution taking on a data plane: the Claude apps gateway provides
  "inline per-request spend metering with daily/weekly/monthly caps per
  user/group/org" — cross-ref ADR-0003 and the pinned upstream source. GIP does
  not alter or independently claim the upstream database-outage posture.
- Metering `max` mode removes the incentive to tamper with telemetry (a
  stopped sidecar no longer lowers enforced usage) without touching the data
  plane (design §8 Phase 2).

## Evidence

- `assets/docs/QUOTA_MONITORING.md:656-668`, `:768`.
- `assets/docs/designs/server-side-metering-design.md:25-28`, §8.
- `assets/docs/APPS_GATEWAY.md`; pinned AWS Samples gateway CDK; `assets/docs/RUNBOOKS.md` §2.
- Commit `ff270be` (#745, `max_session_duration`).
