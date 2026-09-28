# ADR-0025: Accept the validated OIDC bearer on verified telemetry ingress

## Status

Superseded by [ADR-0035](0035-mirror-upstream-claude-apps-gateway.md), 2026-08-15.

## Context

Per-user CoWork telemetry requires identity that the client cannot forge. The
bootstrap server already validates the caller's OIDC token, and the collector's
ALB can validate the same issuer, signature, expiry, and audience on each OTLP
request. In direct-federation deployments, that token may also satisfy the AWS
role trust policy, so it is a capability-bearing credential rather than a
telemetry-only assertion.

## Decision

Verified telemetry may reuse the validated OIDC bearer only when all of these
conditions hold:

- bootstrap delivery uses OIDC bearer validation;
- the collector endpoint is HTTPS;
- the ALB validates issuer, JWKS, expiry, and the configured client audience;
- traffic is routed to the verified-only receiver, separate from aggregate
  shared-token ingress;
- the response is marked `no-store`, the bearer is not logged, and its
  configuration expires before the token;
- caller-supplied identity attributes are discarded and identity is derived
  only from the ALB-validated token claims.

All other central telemetry paths remain aggregate-only. The endpoint never
returns AWS access keys or STS credentials.

## Consequences

The ALB and collector process receive a bearer that may be reusable against the
configured AWS federation trust until token expiry. Compromise of that path has
greater impact than compromise of a telemetry-specific token. Operators must
treat the collector as credential-processing infrastructure and may keep
`OtlpIdentityMode=aggregate` when that risk is unacceptable.

A distinct IdP client and audience, or a server-minted telemetry assertion,
would narrow capability but requires a second client token flow that CoWork does
not currently provide. That remains the preferred future design if the client
can request or exchange for a telemetry-scoped token.
