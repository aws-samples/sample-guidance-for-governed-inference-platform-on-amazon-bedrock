# ADR-0026: Keep enforcement state inside each inference cell

## Status

Accepted, 2026-07-15. Supersedes the multi-account hub-centralization portions
of [ADR-0019](0019-multi-account-network-isolation-doc-first.md); its network-
isolation decision remains accepted.

## Context

ADR-0019 placed the collector, quota ledger, and analytics in a shared-services
hub. That couples cell availability and enforcement, moves identifiable telemetry
outside its residency boundary, and conflicts with Wave 4 Axiom A12. Wave 4B
multi-account building blocks are pending and have not been deployed or validated.

## Decision

Each inference account and residency boundary is an independently operable cell.
It owns quota and metering state, the quota API, gateway policy and database state,
and identifiable telemetry and analytics. Bedrock invocation logs remain in the
calling account and Region. Residency-bound cells use geographic CRIS profiles;
`global.*` profiles are rejected.

Management accounts may apply organization guardrails and coordinate deployment.
Reporting accounts may receive only approved aggregates with no email, principal
ARN, session ID, prompt, response, or unapproved resource identifier. Gateway and
credential-process budgets remain separate until a reconciler is proven.

This ADR defines placement, not implemented Wave 4B capabilities. Versioned cell
manifests, organization policy, StackSet automation, managed entitlements, regional
OAM, aggregate export, and a two-account pilot remain separately gated.

## Alternatives considered

- Central quota ledger: rejected because cross-account DynamoDB capability does
  not justify moving hard-enforcement state or sharing its failure domain.
- Central identifiable OTEL collector and analytics: rejected because transport
  reachability does not satisfy residency or identity-boundary requirements.
- Central gateway and unified budget: rejected because gateway state is part of
  the inference path and no budget reconciler is proven.
- `global.*` CRIS for simpler routing: rejected for residency-bound cells.
- Defer all multi-account guidance: rejected; current single-account stacks can
  be deployed separately while multi-account behavior remains unvalidated.

## Consequences

Cell deployment and packaging repeat per account today. Central dashboards and
payer reports intentionally have no per-user drill-down. Changing that boundary
requires an ADR that supersedes A12. A cell can continue enforcing quota when
another cell or central reporting is unavailable. Any future cross-account
exporter must fail closed on schema or classification violations and may contain
approved aggregates only.

## Evidence

the internal project operating axioms; an internal delivery record;
the internal delivery ledger; `assets/docs/MULTI_ACCOUNT.md`.
