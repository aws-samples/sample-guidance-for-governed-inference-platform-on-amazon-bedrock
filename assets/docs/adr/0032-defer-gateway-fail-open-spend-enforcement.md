# ADR-0032: Gateway spend-enforcement posture during a PostgreSQL outage (AUD-005)

Status: Superseded by [ADR-0035](0035-mirror-upstream-claude-apps-gateway.md)

> **Historical behavior only:** the retired GIP template added a configurable
> `/readyz` fail-closed topology. The selected upstream CDK owns current health
> and spend-failure behavior; this ADR makes no claim about that implementation.

## Context

The customer-readiness audit found (AUD-005, gate G2) that Claude Apps Gateway
spend enforcement fails **open** during a PostgreSQL outage in the default
topology: the gateway keeps serving traffic when its spend-tracking database is
unreachable (customer-readiness audit finding AUD-005, citing the template warning as it
then stood in `deployment/legacy/claude-apps-gateway.yaml`). The finding was
judged vendor (gateway product) behavior, not safely changeable in this repo
without a supported gateway setting and live validation, and was deferred — but
without an ADR at the time. This ADR repairs the record.

## Decision

Defer a code fix. Keep the template warning and recommend Multi-AZ PostgreSQL
for production. Record the carve-out explicitly: the gateway's spend enforcement may be
described as **fail-closed only when the load balancer gates traffic on the
gateway's database-aware `/readyz` probe**; a `/healthz`-gated topology remains
fail-open during a database outage. Customer-facing "hard cap" language must
stay qualified accordingly.

## Alternatives considered

1. **Patch gateway behavior in this repo** — rejected at audit time: the
   fail-open behavior is gateway product behavior; an unsupported change risked
   breaking inference continuity without live validation.
2. **Silently describe enforcement as fail-closed** — rejected; the claim is not
   valid without a `/readyz`-gated topology.

## Consequences

- At audit time, default deployments had a spend-enforcement gap during
  database outages; live outage behavior was never tested.
- **Status update (2026-07-29):** post-hoc review found the deferral was
  already partially remediated: Wave 4 commit `45d22ca` flipped the ALB health
  check to the database-aware `/readyz` probe unconditionally, making the
  deployed default fail-closed. A later change then parameterized the posture (`SpendEnforcementPosture:
  fail-closed | fail-open`, default `fail-closed` to preserve deployed
  behavior), verified `/readyz` semantics against Anthropic's public gateway
  deployment doc (database-aware readiness; gateway itself remains fail-open —
  the LB topology decides the posture), and aligned `APPS_GATEWAY.md` /
  `FAILURE_POSTURE.md` hard-cap language with that carve-out. Live
  database-outage behavior in both postures remains untested
  (`assets/docs/LIVE_VALIDATION.md`).

## Evidence

- Historical posture and semantics: `deployment/legacy/claude-apps-gateway.yaml`
  (`SpendEnforcementPosture` parameter, default `fail-closed`).
- Customer-facing posture language: [FAILURE_POSTURE.md](../FAILURE_POSTURE.md),
  [APPS_GATEWAY.md](../APPS_GATEWAY.md).
- Untested live outage behaviour: [LIVE_VALIDATION.md](../LIVE_VALIDATION.md),
  [OPEN_ITEMS.md](../OPEN_ITEMS.md) OI-2.
- Original finding AUD-005 came from an internal customer-readiness audit that is not
  published in this repository.
