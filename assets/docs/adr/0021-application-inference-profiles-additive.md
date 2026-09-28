# 0021 — Application Inference Profiles as optional team/cost-center attribution (additive), per-user AIPs rejected

Status: Accepted · 2026-07-13 · Wave 3 lane E-M1
Research: an internal research memo (F1–F12), an internal research memo

## Context

Per-user cost attribution already works with zero client changes: the STS
session name (email) lands in CUR 2.0 `line_item_iam_principal`
(`assets/docs/COST_ATTRIBUTION.md` §1), a pattern AWS natively endorsed in the
April 2026 granular-cost-attribution launch (R4 F5). Its gaps: team spend is
not visible in Cost Explorer without Athena, and Direct-STS session names are
client-asserted unless `SessionNameBinding` is enabled (ADR-0015). Bedrock
Application Inference Profiles (AIPs) attach admin-controlled cost-allocation
tags to every invocation made through the profile ARN, flowing to **both**
Cost Explorer and CUR (R4 F1). `gip init` already collected per-tier AIP ARNs
(`init.py`, `validators.py:352`, `config.py:184-186`) but packaging never
consumed them (`test_package_structure.py` xfail — R4 F11).

## Decision

1. **Adopt AIPs as an optional, additive team/cost-center attribution layer.**
   `gip package` writes per-tier AIP ARNs into the distributed Claude Code
   settings, overriding CRIS tier defaults:
   haiku -> `ANTHROPIC_SMALL_FAST_MODEL` + `ANTHROPIC_DEFAULT_HAIKU_MODEL`,
   sonnet -> `ANTHROPIC_DEFAULT_SONNET_MODEL`, opus -> `ANTHROPIC_DEFAULT_OPUS_MODEL`.
   `ANTHROPIC_MODEL` keeps its alias so resolution routes through the
   overridden tier chain (Claude Code accepts AIP ARNs as model identifiers
   and treats those pins as admin-managed — R4 F12). Overrides apply even when
   the tier defaults are gated off (managed settings without model lock): the
   ARNs are admin attribution routing, not a user model preference.
2. **Reject per-user AIPs.** The account quota is 1,000 profiles
   (`L-40EC9882`, verified live — R4 F6); profile count = users x tiers makes
   per-user structurally infeasible, and AWS explicitly recommends team/cost-
   center tagging with IAM principal attribution for per-user (R4 F1).
3. **Not a replacement for session-name attribution.** AIP granularity is per
   usage type per day, aggregated dollars — no per-request/per-user detail
   (R4 F1). Both signals land on the same CUR line item (R4 F5), so the layers
   compose.
4. **Other harnesses (OpenCode/Codex/Pi/Aider) do NOT inherit AIP ARNs.**
   AIP-ARN-as-model-ID is verified for Claude Code only (R4 F12); the generated
   harness configs key models by CRIS IDs and their handling of opaque
   profile ARNs is unverified. Per-user attribution still covers them via the
   shared credential process (ADR-0007). Revisit per harness with live
   verification before extending.

## Alternatives considered

- **Per-user AIPs — rejected** (quota-infeasible, AWS-discouraged; R4 F1/F6).
- **Replace session names with AIPs — rejected**: requires client config per
  harness, loses per-user granularity, and daily-aggregate dollars cannot
  drive quota/metering (R4 verdict).
- **Session tags via IdP token (`COST_ATTRIBUTION.md` §3) as the team layer —
  not preferred as default**: requires per-customer IdP token customization;
  AIPs need none.
- **Shipping ABAC enforcement (`bedrock:InferenceProfileArn` + team tag, R4
  F7/F8) now — deferred**: enforcement breaks every non-overridden model path
  (e.g. `/model` picks); opt-in only after all paths route through AIPs.

## Consequences

- One AIP set per team means packaging is per-team (run `gip package` per team
  profile). Model-version churn multiplies profiles; lifecycle automation
  (T-S lanes) is the mitigation.
- Without ABAC enforcement, which profile a client uses is client config —
  AIP attribution is as spoofable as unbound session names (R4 risk 6).
- Bedrock invocation logs carry the full AIP ARN in `modelId` (LT2 live), so
  server-side metering can attribute team usage with no schema change.
- LV-3 live-verified the packaged Claude Code AIP pin on 2026-08-14: the client
  returned `pong`, and the invocation log carried the full AIP ARN. The test
  used the required explicit opaque-AIP opt-out from Anthropic-only IAM
  resource matching; the default restriction was restored afterward.
- Keep AIP sources geographic (`us./eu./jp./apac.`); `global.*` sources can
  fail strict `AllowedBedrockRegions` conditions (R4 F10).
- Tag activation is not retroactive and lags up to 24h+24h; live CUR/Cost
  Explorer verification is a residual orchestrator item (R4 sandbox plan).

## Evidence

- Implementation: `source/governed_inference_platform/cli/commands/package.py`
  (`_create_claude_settings` AIP override block).
- Tests: `source/tests/integration/test_package_structure.py`
  (`test_inference_profile_arns_override_models`,
  `test_inference_profile_opus_arn_overrides_opus_tier`,
  `test_inference_profile_arns_apply_without_model_selection`).
- Docs: `assets/docs/COST_ATTRIBUTION.md` §5.
- Prior wiring: `init.py` AIP prompts, `validators.py:352-371`,
  `config.py:184-186`.
