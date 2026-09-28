# ADR-0028: Discard quota Lambda sizing without equivalent authenticated traffic

Date: 2026-07-16
Status: Accepted

## Context

EXP-QUOTA-SIZING proposed comparing 128 MB x86_64, 256 MB arm64, and
512 MB arm64 using client p99 across 200 requests with at least 20 cold starts.
Response parity and no more than 10% Lambda duration-cost growth were hard gates
(an internal research memo).

## Decision

Discard the experiment without running ARM candidates. The live baseline issued
200 SigV4 `GET /check` requests using a synthetic assumed-role session, but every
response was `missing_identity`. The handler returned before quota table access,
so its client p99 did not represent the authenticated quota path. Simulating an
event would violate the live-request contract.

Keep the production default unchanged: 128 MB and implicit x86_64
(`deployment/infrastructure/quota-monitoring.yaml:467-490`).

## Alternatives Considered

- Compare the three variants on the missing-identity path: rejected as a
  non-equivalent metric.
- Inject identity by invoking Lambda directly: rejected because it omits API
  Gateway and cannot produce the required end-to-end `/check` metric.
- Fix IAM event parsing inside this experiment: rejected as a separate behavior
  change that would invalidate the baseline-first sizing comparison.

## Consequences

No latency or cost winner is claimed. A future sizing experiment must first have
an independently verified live IAM or OIDC request that reaches quota policy and
usage reads with synthetic identity/data.

## Evidence

an internal attempt record and
an internal delivery record.
