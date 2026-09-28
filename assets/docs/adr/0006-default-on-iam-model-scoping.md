# ADR-0006: Server-side model governance via default-on IAM resource scoping

Status: Accepted · Date: 2026-07-07

## Context

The per-user IAM policy granted `bedrock:InvokeModel*` on `foundation-model/*`
and `inference-profile/*`, restricted only by region — a user with valid STS
credentials could invoke any enabled model, including non-Anthropic ones,
regardless of `managed-settings.json` model locks (`REVIEW.md:23-32` finding
#1). Option (a) from that finding — a template parameter narrowing resources
to Anthropic ARNs, as the apps-gateway task role already does — was
implemented in lane A (`81982b2`, merged `7f58bb1`).

## Decision

Add a `RestrictToAnthropicModels` parameter (default `'true'`) to every
bedrock-auth template. When `'true'`, the invoke statement's resources narrow
to `foundation-model/anthropic.*` and `inference-profile/*.anthropic.*`.
Opaque `application-inference-profile/*` ARNs are allowed only in the
unrestricted branch, because they cannot be pattern-matched to an underlying
model family.

## Alternatives considered

- **Default-off.** Rejected after the enterprise hardening review: it preserved
  broad model access for every deployment that did not manually override the
  parameter, so client-side model locks could still be bypassed. Admins with
  approved non-Anthropic or application-inference-profile use cases can set the
  profile field `restrict_to_anthropic_models=false` and redeploy.
- **IAM condition-key approaches.** Rejected: for `InvokeModel`, model
  identity is expressed only in the resource ARN — there is no per-model
  condition key to match on. And `application-inference-profile` IDs are
  opaque (random IDs, no model name), so they cannot safely be included in the
  Anthropic-only branch.
- **Rely on the apps gateway for model entitlements.** Not a substitute:
  valid only for gateway adopters (option (b) in `REVIEW.md:29-32`); this
  parameter hardens the credential-process path itself. See ADR-0003.

## Consequences & optimizations

- Default-on means new and updated auth stacks enforce Anthropic model scoping
  unless an admin explicitly opts out.
- Application inference profiles require explicit opt-out
  (`restrict_to_anthropic_models=false`) until a narrower allow-list is added.
- Regression coverage: `source/tests/test_model_restriction_param.py` asserts
  both parameter branches across all seven auth templates;
  `source/tests/test_cloudformation.py` updated.

## Evidence

- Commit `81982b2` (merge `7f58bb1`).
- `deployment/infrastructure/bedrock-auth-okta.yaml:54-65`, `:78`, `:110-132`.
- `REVIEW.md:23-32`; gateway task-role precedent `claude-apps-gateway.yaml`
  (`d6cae33`: "scoped to anthropic.* models").
