# ADR-0004: Gateway config via CFN-rendered env var with `${ENV}` secret placeholders

Status: Superseded by [ADR-0035](0035-mirror-upstream-claude-apps-gateway.md) · Date: 2026-07-07

## Context

The gateway needs a `gateway.yaml` containing values from three sources with
different lifecycles: static parameters (URLs, OIDC issuer), **values that
exist only after the stack deploys** (the RDS endpoint —
`DbEndpoint: !GetAtt Database.Endpoint.Address`,
`claude-apps-gateway.yaml:467`), and **secrets** (OIDC client secret, JWT
signing key, Postgres password) that must never appear in config text, task
definitions, or CloudFormation state.

## Decision

CloudFormation renders the full `gateway.yaml` into a task-definition
environment variable (`GATEWAY_CONFIG_YAML`,
`claude-apps-gateway.yaml:441-468`) via `!Sub`; the container start command
writes it to `/tmp/gateway.yaml` and execs the gateway (`:432-435`). Secret
values are referenced as literal `${ENV_VAR}` placeholders (`${!...}` escapes
in `!Sub`: `:452`, `:456`, `:461`) which the gateway natively expands at
config load (`:428-431` comment); the actual values are injected by ECS from
Secrets Manager (`Secrets:` block, `:469-476`). Commit `d6cae33`.

## Alternatives considered

- **Bake `gateway.yaml` into the container image.** Rejected: chicken-and-egg
  — the Postgres endpoint is a `!GetAtt` on a resource in the same stack, so
  it cannot be known at image-build time; every config change would also mean
  an image rebuild + ECR push + redeploy instead of a stack update. Secrets in
  an image layer are also unacceptable.
- **SSM Parameter Store document fetched at boot.** Rejected: same
  post-deploy-endpoint problem (something must still template the endpoint
  into the parameter), plus an extra boot-time dependency and IAM surface for
  a ~30-line file; SecureString would split the config across two systems.
- **EFS-mounted config file.** Rejected: mount targets, AZ coupling, and an
  out-of-band write path for a file CloudFormation can render directly; drift
  between file and stack parameters becomes possible.

## Consequences & optimizations

- Config changes are **stack updates, not image rebuilds**: extending the
  rendered YAML (e.g. adding a `managed.policies` block for per-group model
  allow-lists) was a legacy template edit + deployment
  (`assets/docs/APPS_GATEWAY.md:199-220`).
- No secret material ever appears in the template, task-definition JSON, or
  CloudFormation console — only `${ENV_VAR}` placeholder strings.
- Cost: the config is visible in the task definition's environment (metadata,
  not secret) and its size is bounded by the env-var limit; fine for this
  config's scale.

## Evidence

- `deployment/legacy/claude-apps-gateway.yaml` (historical implementation).
- Commit `d6cae33` ("gateway.yaml is rendered by CloudFormation into the task
  definition with ${ENV} placeholders so secrets are injected by ECS and
  never stored in the config").
