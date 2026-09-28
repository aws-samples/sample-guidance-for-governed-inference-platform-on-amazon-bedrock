# ADR-0018 — Model discovery: codegen-PR as system of record, additive `extra_models` overlay as escape hatch

Status: Accepted · Wave 3, lane E-S2 · 2026-07-09 · Research: an internal research memo

## Context

The Claude model catalog is hardcoded (`source/governed_inference_platform/models.py`
`_CLAUDE_MODELS_RAW`), and it is not just IDs: tier fallback chains
(`MODEL_TIER_PREFERENCES`), residency prefixes, rate-limit families, and the CFN
`AllowedBedrockRegions` defaults all consume it. When AWS launches a model, users
can't select it until a repo release ships — days to weeks. Meanwhile Bedrock now
publishes machine-readable lifecycle dates (`modelLifecycle.legacyTime` /
`publicExtendedAccessTime` / `endOfLifeTime`, live-verified 2026-07-08): catalog
models the platform ships were entering premium pricing and Legacy with zero
repo-side visibility.

## Decision

1. **Codegen → repo PR is the system of record.** `gip models check --propose`
   synthesizes ready-to-paste `_CLAUDE_MODELS_RAW` entries from the live data the
   drift check already fetches (`catalog_check.py:build_proposals` /
   `render_models_py_snippet`). Proposals carry mandatory `TODO(review)` markers
   for what live data cannot decide (tier placement, rate limits, residency,
   naming) and never modify files on their own.
2. **A deliberately minimal, additive `extra_models` profile overlay** is the
   escape hatch (`config.py:Profile.extra_models`,
   `models.py:validate_extra_models`/`get_effective_models`): entries may never
   shadow a catalog key, base model ID, or CRIS profile ID (validation fails
   loudly); they are excluded from tier fallback chains; display names are
   flagged ` (overlay)`. Selectable in `gip init`/`package` in minutes.
3. **Lifecycle alerting is a small optional stack**
   (`deployment/infrastructure/model-lifecycle.yaml`, ~$0/mo): daily Lambda joins
   lifecycle dates against a deploy-seeded tracked-models SSM parameter, SNS
   ladder (legacy / premium-30d / EOL-60/30/7d), permissive `aws.health` Bedrock
   passthrough rule, Errors>0 alarm (A5 fail-visible).

## Alternatives considered

- **Full runtime overlay catalog file — REJECTED.** Forks the single source of
  truth, bypasses the tier/residency/CI test surface, creates a second file
  format to maintain (violates A1's spirit). The overlay's restricted additive
  semantics exist precisely to avoid this.
- **Auto-PR codegen bot — deferred.** An auto-generated entry cannot safely
  self-place into tier chains; human review is the point of the PR path.
- **`PinnedModelAllowlist` IAM mode — deferred (design recorded).** Today
  `RestrictToAnthropicModels=true` wildcards `anthropic.*`, so new models are
  auto-allowed and legacy premium-priced models stay allowed until EOL. Keeping
  the wildcard default preserves customer choice (A7) and avoids a stack update
  per model release. The deferred design (R14 §4): a CommaDelimitedList CFN
  parameter (default empty = wildcard) whose entries replace the two wildcard
  resource ARNs with explicit model/profile ARNs, regenerated from
  catalog+overlay entries whose live status is ACTIVE. Trade-offs: every
  rotation becomes a stack update; users see raw `AccessDeniedException` — only
  enable together with the alerting stack.

## Consequences

- New-model latency drops to minutes (overlay) without forking the catalog;
  the repo catalog stays authoritative and upstream-diffable (data-only diffs).
- Overlay models never become silent tier defaults; a stale overlay entry fails
  validation as a duplicate once the catalog catches up, forcing cleanup.
- Known metering gap recorded, not fixed here: `pricing.py` has no
  legacy-premium prices → under-metering during extended access (metering lane).

## Evidence

`catalog_check.py:collect_lifecycle_warnings/build_proposals`;
`models.py:validate_extra_models/get_effective_models`;
`deploy.py:compute_tracked_models`; tests: `tests/test_catalog_check.py`,
`tests/test_models.py:TestExtraModelsOverlay`,
`tests/test_model_lifecycle_lambda.py`, `tests/test_model_lifecycle_template.py`.
