# ADR-0008: Non-interactive init via an answers file mirroring the wizard's config dict

Status: Accepted · Date: 2026-07-07

## Context

`gip init` was a ~30-question interactive wizard with no non-interactive
path; config changes required replaying it, blocking CI/GitOps-managed
deployments (`REVIEW.md:110-113` finding #14). Lane C added the
non-interactive mode (`4665024`, merged `b710d1a`).

## Decision

`gip init --from-file <answers.yaml|json>` consumes an answers file whose
structure **mirrors the wizard's internal config dict** — "the same keys the
interactive wizard writes and `InitCommand._save_configuration` reads"
(`source/governed_inference_platform/cli/commands/init_answers.py:1-14`).
Omitted values take the same defaults as pressing Enter through the wizard;
unknown keys are a hard error via an `ALLOWED_KEYS` whitelist (typo
protection, `init_answers.py:39-41`). `--export-answers` rebuilds an answers
file from a saved profile (`init_answers.py:776`, `:893`), giving existing
wizard-managed deployments a migration path: export once, commit to git,
manage declaratively thereafter. Flags are mutually exclusive
(`cli/commands/init.py:135-142`).

## Alternatives considered

- **New declarative schema (independent of the wizard).** Rejected: two
  representations of the same configuration drift — every wizard change would
  need a schema mapping, and any missed mapping silently diverges CI deploys
  from interactive ones. Mirroring the internal dict is zero-drift by
  construction: both paths feed the identical `_save_configuration` keys.
- **Terraform/CDK rewrite of the admin plane.** Rejected: the entire solution
  is CloudFormation templates driven by the profile
  (`deployment/infrastructure/`, `CLAUDE.md` Architecture); an IaC rewrite is
  a fork-scale divergence that would end upstream tracking (ADR-0001) for a
  problem an answers file solves.

## Consequences & optimizations

- CI has no TTY: "No questionary prompt is ever issued on this path"
  (`init_answers.py:13`).
- Only two fields have no possible default and must always be provided for
  OIDC (`okta.domain`, `okta.client_id`, `init_answers.py:10-11`); everything
  else has wizard-parity defaults.
- Cost accepted: the answers schema is coupled to wizard internals — a wizard
  key rename is a breaking answers-file change. Mitigated by the whitelist
  erroring loudly and by round-trip tests
  (`source/tests/cli/commands/test_init_from_file.py`, 396 lines).
- Downstream consumer already planned: the gateway is configured via profile
  fields "or, once available, the answers-file mode"
  (`assets/docs/APPS_GATEWAY.md:95-98`).

## Evidence

- Commit `4665024` (merge `b710d1a`).
- `init_answers.py:1-80`, `init.py:102-142`,
  `assets/docs/CLI_REFERENCE.md` (+78 lines), `REVIEW.md:110-113`.
