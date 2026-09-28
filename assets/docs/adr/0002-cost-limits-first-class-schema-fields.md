# ADR-0002: Cost limits as first-class QuotaPolicy schema fields

Status: Accepted · Date: 2026-07-07

## Context

Cost-based limits (USD budgets, the init wizard's recommended mode) were
stored upstream as raw DynamoDB attributes written outside the `QuotaPolicy`
dataclass. That side-attribute design already caused upstream bug #748:
enforcement read `cost_usd` (an in-memory variable name) instead of the actual
DDB attribute `estimated_cost`, and policy reads dropped
`monthly_cost_limit`/`daily_cost_limit` entirely (`e5e0cca` commit message,
bugs 1–3). Our review found a second, independent dead path: the quota_check
env-default branch required a *token* limit to activate, so cost-mode
deployments without fine-grained DDB policies enforced nothing — **verified
by** reading the pre-fix condition
(`git show 9d38075^:deployment/infrastructure/lambda-functions/quota_check/index.py`,
line 335: `if not ENABLE_FINEGRAINED_QUOTAS and MONTHLY_TOKEN_LIMIT > 0:`;
cost mode sets `MONTHLY_TOKEN_LIMIT=0` → condition false → no policy →
unlimited). Full four-part breakage is documented in `9d38075` and
`REVIEW.md:15`.

## Decision

Promote `monthly_cost_limit`/`daily_cost_limit` to first-class `QuotaPolicy`
schema fields (`source/governed_inference_platform/models.py:1409-1415`), with
Decimal-safe `to/from_dynamodb_item`, create/update/export/bulk-import
round-trip (`source/governed_inference_platform/quota_policies.py:117-118`,
`:278-284`, `:508-509`, `:578-579`), and carry cost limits through the
env-default path (`quota_check/index.py:339-349`). Commit `9d38075`.

## Alternatives considered

- **Keep raw side-attributes.** Rejected: attribute-name mismatches (#748) and
  silent budget loss on export/import are direct consequences of writes that
  bypass the schema (`models.py:1409-1413` comment records this).
- **Separate cost-policy table.** Rejected: duplicates the
  user > group > default precedence already implemented over `QuotaPolicies`
  (`quota_check/index.py:333-335` docstring) and adds a second DDB read on the
  credential-issuance hot path.
- **Env-only limits (stack parameters, no per-user budgets).** Rejected:
  fine-grained per-user/group policies are an existing feature; env-only would
  remove per-user budgets rather than fix them.

## Consequences & optimizations

- Init re-run no longer silently resets a cost-mode deployment to token mode
  (`test_rerun_preserves_cost_mode_fields`,
  `source/tests/cli/commands/test_init_quota_roundtrip.py`).
- Regression tests: `source/tests/test_quota_check_cost_env.py`,
  `TestQuotaPolicyCostLimits`.
- Follow-up closed in lane B: `gip quota set` cost writes now route through
  `QuotaPolicyManager.update_policy` instead of a raw `UpdateExpression`
  (`REVIEW.md:71-74` finding #8; `df07bbf`,
  `source/tests/cli/commands/test_quota_set_cost_routing.py`).

## Evidence

- Commits: `9d38075` (this fork), `e5e0cca` (upstream #748).
- `REVIEW.md:15`, `quota_check/index.py:339-349`, `models.py:1401-1415`.
