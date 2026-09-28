# 0034 — Keep quota checks single-attempt and fail-closed

Status: Accepted · 2026-08-12 · Wave 9

## Context

Credential issuance and periodic re-checks call the quota API once with a
bounded timeout. A transient failure therefore denies new credentials in the
default fail-closed mode. Wave 5 tested bounded retries: Go improved, but the
legacy Python Requests transport could not guarantee an absolute deadline
without leaking blocked worker threads and sockets. Two implementations failed
review and EXP-QUOTA-RETRY was discarded.

## Decision

Keep the production path single-attempt and fail-closed until both credential
providers have a cancellable transport with a proven absolute deadline. Do not
ship asymmetric Go-only retries: the legacy provider remains a supported
fallback under ADR-0013 and quota behavior must stay in parity.

Add operator alarms for quota-check Lambda errors, quota-monitor Lambda errors,
and quota API 5XX responses. Keep `quota_fail_mode=open` as the explicit
break-glass availability choice; unknown values continue to resolve closed.

## Alternatives considered

- **Retry only in Go:** rejected because it creates provider-dependent access
  behavior during the same outage.
- **Detached timeout worker in Python:** rejected by two reviews because the
  request can outlive the credential process and leak resources.
- **Fail open by default:** rejected; it defeats quota enforcement during the
  exact dependency failure being handled.

## Consequences and reopen condition

A transient quota-service failure can temporarily deny credential issuance.
Operators receive alarms and can invoke the documented break-glass mode. Reopen
retry work only with a cancellable Python transport and tests proving deadline,
worker, and socket cleanup for slow-drip responses.

Evidence: discarded retry experiment results (internal, not published),
`source/go/internal/quota/quota.go`, and `assets/docs/FAILURE_POSTURE.md`.
