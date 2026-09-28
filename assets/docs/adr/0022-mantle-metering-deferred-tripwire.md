# ADR-0022: Mantle per-user metering deferred; explicit deny + account-level tripwire

Status: Accepted; tripwire live-verified · Date: 2026-07-13 · Live result: 2026-08-14

## Context

Axiom A4 (ADR-0012 Decision 5): no endpoint reachable by governed credentials
may be invisible to metering. E-A4 put an explicit `DenyBedrockMantleEndpoint`
(`bedrock-mantle:*`, `Resource: '*'`) in every governed Bedrock credential
policy (9 templates, locked by `source/tests/test_mantle_fail_closed.py` and
`test_auth_template_parity.py:569`). E-M2 had to decide whether to extend
metering to Mantle or keep the fail-closed scope.

Live evidence (an internal research memo, sandbox probe 2026-07-08)
confirms R12: a Mantle `/v1/chat/completions` call produced **no Bedrock model
invocation log event** (the platform's metering source), was **invisible to
CloudTrail management events** (inference is a data event), and appeared only
in the `AWS/BedrockMantle` CloudWatch namespace (`Inferences`,
`TotalOutputTokens`) with a `Project` dimension and **no user identity**. CUR
attribution for `bedrock-mantle` IAM principals is announced but not shipped
(R12 row 4).

## Decision

1. **Per-user Mantle metering is rejected for now.** No server-side identity
   source exists: no invocation logs, no identity dimension in
   `AWS/BedrockMantle`, CloudTrail data events carry the caller but no token
   counts (same reason CloudTrail was rejected in ADR-0005). Building a
   partial meter would create false confidence; the deny is the control.
2. **The explicit-deny posture stays** (unchanged from E-A4) until AWS ships
   invocation-logging/attribution parity for Mantle or an equivalent metering
   source satisfying A4.
3. **A fail-visible tripwire ships**: `MantleEndpointUsageAlarm` in
   `quota-metering.yaml` — a Metrics Insights alarm on
   `SELECT SUM(Inferences) FROM "AWS/BedrockMantle"` (>=1 in 5 min), routed to
   `AlarmTopicArn` when set. Since governed credentials are denied and metering
   cannot see Mantle, any datapoint means ungoverned use in that
   account/region. The query aggregates across all `Project` dimension values;
   a dimensionless metric alarm would never match Mantle's metrics.

LV-4 live-verified this path in `us-east-1` on 2026-08-14: one controlled
Mantle call moved the alarm from `OK` to `ALARM`, and stopping calls returned
it to `OK`.

## Alternatives considered

- **Extend `sidecar_monitor` to Mantle events.** Rejected: it joins CloudTrail
  *management* events against OTEL; Mantle inference is a *data* event —
  invisible to `LookupEvents` (LT-2). Only low-signal calls like `ListModels`
  would match.
- **CloudTrail data-event trail on `AWS::BedrockMantle::Project`.** Deferred:
  gives caller identity per inference but costs money for traffic that should
  not exist, and still lacks token counts. Documented as the escalation path
  when the tripwire fires (R12 recommendation 3).
- **Tripwire in `quota-monitoring.yaml`.** Rejected: that stack is
  single-region; `AWS/BedrockMantle` metrics are regional and Mantle has no
  cross-region inference profiles. `quota-metering.yaml` deploys once per
  allowed Bedrock region — matching the coverage surface — and already has the
  alarms section + `AlarmTopicArn` wiring.

## Consequences

- Deployments without the metering stack have the deny but no tripwire; the
  SCP backstop (MULTI_ACCOUNT.md SCP-2) remains the org-wide control.
- `gip deploy` wires `AlarmTopicArn` to the quota alert topic in the quota
  region only; metering stacks in other regions have console-visible alarms
  unless a regional topic is supplied manually.
- Revisit trigger: AWS ships "IAM principal attribution for bedrock-mantle"
  (announced as coming soon, R12 row 4) or Mantle invocation logging — then
  E-M2 reopens as extend-metering and this ADR is superseded.

## Evidence

- an internal research memo (parity matrix rows 2-5; sources read 2026-07-08)
- Live probe against a development sandbox account (internal delivery record, not published)
- `deployment/infrastructure/quota-metering.yaml` (`MantleEndpointUsageAlarm`)
- `source/tests/test_mantle_fail_closed.py`; `test_auth_template_parity.py:569`
