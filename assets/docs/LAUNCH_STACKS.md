# Launching Stacks from the AWS Console

Every template in `deployment/infrastructure/` is a plain CloudFormation
template. This guide shows how to publish them to your own S3 bucket and
deploy them one stack at a time with **quick-create links** — the one-click
console experience (the AWS equivalent of a "Deploy to Azure" button). Each
standalone template carries `AWS::CloudFormation::Interface` metadata, so the
console shows grouped, plain-English parameter forms instead of a raw
alphabetical list.

This is the per-stack flavor of
[Path 3 — By hand (CloudFormation console)](DEPLOYMENT_PATHS.md#path-3-by-hand-cloudformation-console).
Read the caveats at the end before choosing it: it deploys individual stacks,
not the whole platform.

## Prerequisites

- **An S3 bucket you own** to host the templates. The bucket must be readable
  by whoever clicks the links (same account is simplest; a bucket policy works
  for multi-account).
- **AWS CLI configured** on the machine that runs the publish step (this is
  the only place the CLI is needed — deployers only need the console).
- **The two manual prerequisites every path shares**: an OIDC application
  registered in your identity provider
  ([provider guides](providers/README.md)) and Anthropic model access enabled
  in the Amazon Bedrock console for your regions.
- **Change-review inputs**: [RESOURCE_INVENTORY.md](RESOURCE_INVENTORY.md)
  lists per-stack resources and IAM actions, written for SCP pre-clearance
  and change tickets.

## Step 1 — Publish the templates

```bash
scripts/publish-templates.sh <your-bucket> [prefix] [region]
# example:
scripts/publish-templates.sh my-cfn-templates gip-templates us-west-2
```

The script:

1. Runs `aws cloudformation package` on the templates that bundle local
   Lambda source (`quota-monitoring`, `quota-metering`, `model-lifecycle`,
   `skills-registry` and the CLI-driven memory stack), uploading
   the code bundles under `<prefix>/artifacts/` so the published copies
   deploy as-is from the console.
2. Uploads every template to `s3://<your-bucket>/<prefix>/`.
3. Prints ready-to-paste markdown: a quick-create link plus an
   `aws cloudformation create-stack` fallback for each standalone template.

Re-run it after pulling template updates, then update your stacks with the
same template URLs.

## Step 2 — Deploy from the console

Each quick-create link opens the CloudFormation console on the final review
page with the template URL and a suggested stack name pre-filled:

```text
https://<REGION>.console.aws.amazon.com/cloudformation/home?region=<REGION>#/stacks/create/review?templateURL=https://<YOUR_BUCKET>.s3.<REGION>.amazonaws.com/gip-templates/<template>.yaml&stackName=<suggested-name>
```

Fill in the parameter form (grouped and labeled per template), acknowledge
IAM capabilities, and create the stack. That's the whole deployment for that
stack.

## Standalone templates

Deploy in roughly the order listed — later rows take values that earlier
stacks output (copy them from the earlier stack's **Outputs** tab).

| Template | What it deploys | Suggested stack name | Depends on / notes | Link |
|---|---|---|---|---|
| `bedrock-auth-okta.yaml` | Okta → Bedrock federation (IAM role, optional identity pool) | `gip-auth-okta` | None. Deploy exactly one `bedrock-auth-*` stack; its `ConfigurationJson` output is the complete client config | [launch][l-okta] |
| `bedrock-auth-azure.yaml` | Microsoft Entra ID (Azure AD) → Bedrock federation | `gip-auth-azure` | Same as above | [launch][l-azure] |
| `bedrock-auth-auth0.yaml` | Auth0 → Bedrock federation | `gip-auth-auth0` | Same as above | [launch][l-auth0] |
| `bedrock-auth-google.yaml` | Google → Bedrock federation | `gip-auth-google` | Same as above | [launch][l-google] |
| `bedrock-auth-generic.yaml` | Any OIDC provider (PingFederate, Keycloak, ...) → Bedrock federation | `gip-auth-generic` | Same as above | [launch][l-generic] |
| `bedrock-auth-cognito-pool.yaml` | Cognito user pool → Bedrock federation | `gip-auth-cognito-pool` | An existing user pool — deploy `cognito-user-pool-setup` first | [launch][l-cognito-auth] |
| `bedrock-auth-idc.yaml` | IAM Identity Center → Bedrock access role | `gip-auth-idc` | IAM Identity Center enabled in the account | [launch][l-idc] |
| `cognito-user-pool-setup.yaml` | Cognito user pool + hosted UI (skip external IdP setup) | `gip-user-pool` | None | [launch][l-user-pool] |
| `cognito-custom-domain-cert.yaml` | ACM certificate for a Cognito custom domain | `gip-cognito-domain-cert` | Route 53 hosted zone. **us-east-1 only** | [launch][l-domain-cert] |
| `networking.yaml` | VPC + public subnets for the telemetry collector | `gip-networking` | None. Skip if you bring your own VPC | [launch][l-networking] |
| `otel-collector.yaml` | OpenTelemetry collector on ECS Fargate behind an ALB | `gip-monitoring` | VPC/subnets (`networking` outputs or your own), OIDC issuer/client for JWT validation | [launch][l-otel] |
| `claude-code-dashboard.yaml` | CloudWatch dashboard over Claude Code metrics | `gip-dashboard` | Telemetry flowing (deploy `otel-collector` first) | [launch][l-dashboard] |
| `cowork-dashboard.yaml` | CloudWatch dashboard for Claude Desktop (CoWork) events | `gip-cowork-dashboard` | CoWork telemetry flowing | [launch][l-cowork] |
| `logs-insights-queries.yaml` | Saved Logs Insights queries for usage analysis | `gip-logs-insights` | Metrics log group (from `otel-collector`) | [launch][l-queries] |
| `analytics-pipeline.yaml` | Firehose + Athena analytics over the metrics log group | `gip-analytics` | `otel-collector` with `EnableAnalytics=true` | [launch][l-analytics] |
| `quota-monitoring.yaml` | Per-user token/cost quotas, alerts, quota check API | `gip-quota` | Telemetry (`otel-collector`); OIDC issuer/client for API auth | [launch][l-quota] |
| `quota-metering.yaml` | Server-side metering from Bedrock invocation logs | `gip-metering` | `gip-quota` stack (table name/region outputs). Deploy once per allowed Bedrock region | [launch][l-metering] |
| `guardrails-enforcement.yaml` | Bedrock guardrail + account-level enforcement (opt-in) | `gip-guardrails` | None (per account and region) | [launch][l-guardrails] |
| `model-lifecycle.yaml` | Model end-of-life / legacy-pricing alert ladder | `gip-model-lifecycle` | None; optionally reuse an existing SNS topic | [launch][l-lifecycle] |
| `landing-page-distribution.yaml` | Authenticated package-download landing page (ALB + Lambda) | `gip-landing-page` | VPC with public+private subnets, custom domain + ACM, IdP web app. Serves packages produced by `gip package` | [launch][l-landing] |
| `skills-registry.yaml` | Governed skills registry (review sources + approved artifacts) | `gip-skills-registry` | Optional plugins bucket; IdP federation for group-gated roles. See [SKILLS_REGISTRY.md](SKILLS_REGISTRY.md) | [launch][l-skills] |
| `bedrock-agentcore-gateway.yaml` | AgentCore gateway with managed web search for CoWork | `gip-websearch-gateway` | **us-east-1 only** (the only Region `gip` allows for web search). See [WEB_SEARCH.md](WEB_SEARCH.md) | [launch][l-websearch] |

Replace `<YOUR_BUCKET>` and `<REGION>` in the link definitions below with your
values — or simply paste the output of `scripts/publish-templates.sh`, which
fills them in for you.

## Templates without launch links (CLI-driven)

These upload with the rest but are not meaningful as standalone console
deployments:

- `s3bucket.yaml` — artifact bucket for the packaging pipeline. Its only
  parameter is the optional `OrganizationId` org-read grant for StackSet
  targets (default empty/off; see [MULTI_ACCOUNT.md](MULTI_ACCOUNT.md)).
- `distribution.yaml`, `presigned-s3-distribution.yaml` — package-distribution
  buckets; they hold artifacts that only `gip package` / `gip distribute`
  produce.
- `codebuild-windows.yaml` — build backend for the legacy
  `gip package --legacy` pipeline.
- Claude Apps Gateway and Desktop bootstrap are not quick-create templates.
  Use the pinned AWS Samples CDK documented in
  [APPS_GATEWAY.md](APPS_GATEWAY.md) (`scripts/fetch-claude-apps-gateway.sh`
  materializes it).
- `memory-stack.yaml` — opt-in add-on that attaches to an *existing* AgentCore
  gateway (needs its identifier and execution-role outputs). See
  [MEMORY.md](MEMORY.md).
- `cognito-identity-pool.yaml` — legacy standalone identity pool template kept
  for existing manual deployments; the `gip` CLI does not deploy or update it
  (its `OIDCThumbprintList` must be carried through by the operator on every
  update, see the caveat below). Console users should deploy a `bedrock-auth-*`
  stack instead.

## Honest caveats

- **Per-stack only.** Quick-create links deploy one stack at a time. Ordering
  and cross-stack wiring (copying outputs into the next stack's parameters)
  are on you; `gip deploy` does both automatically.
- **Updating an existing `bedrock-auth-*` stack: keep its thumbprints.** The
  auth templates no longer hardcode IAM OIDC provider thumbprints; on a *new*
  stack leave `OidcThumbprintList` empty and IAM retrieves the CA thumbprint
  itself. When you *update* a stack whose provider already has thumbprints, you
  must pass its current list — IAM rejects removing it (the update fails and
  rolls back with `Value at 'thumbprintList' failed to satisfy constraint:
  Member must not be null`; verified live). Read the list with
  `aws iam get-open-id-connect-provider --open-id-connect-provider-arn <arn>`
  and pass it as `OidcThumbprintList=<comma-separated list>`. `gip deploy` does
  this automatically for the `bedrock-auth-*` stacks it manages; for the legacy
  `cognito-identity-pool.yaml` (parameter `OIDCThumbprintList`) the operator
  passes it by hand.
- **Client packaging still needs tooling.** Installers, client binaries, and
  the generated `config.json`/`settings.json` come from `gip package` (or the
  binaries built with `make` in `source/go`; macOS targets need a macOS host) — see the
  [Path 3 packaging notes](DEPLOYMENT_PATHS.md#path-3-by-hand-cloudformation-console).
- **Quota policies are seeded by the CLI.** The quota stack deploys the
  enforcement machinery, but fine-grained user/group policies are managed
  with `gip quota set-user` / `set-group`
  ([QUOTA_MONITORING.md](QUOTA_MONITORING.md)).
- **Published Lambda-backed templates differ from the repo copies.**
  `aws cloudformation package` rewrites their `Code:` references to point at
  the uploaded bundles; review the packaged copy in S3 if your change process
  requires reviewing exactly what deploys.
- **Region constraints.** `cognito-custom-domain-cert` and
  `bedrock-agentcore-gateway` must run in us-east-1; `quota-metering` is
  deployed once per allowed Bedrock region.

[l-okta]: https://<REGION>.console.aws.amazon.com/cloudformation/home?region=<REGION>#/stacks/create/review?templateURL=https://<YOUR_BUCKET>.s3.<REGION>.amazonaws.com/gip-templates/bedrock-auth-okta.yaml&stackName=gip-auth-okta
[l-azure]: https://<REGION>.console.aws.amazon.com/cloudformation/home?region=<REGION>#/stacks/create/review?templateURL=https://<YOUR_BUCKET>.s3.<REGION>.amazonaws.com/gip-templates/bedrock-auth-azure.yaml&stackName=gip-auth-azure
[l-auth0]: https://<REGION>.console.aws.amazon.com/cloudformation/home?region=<REGION>#/stacks/create/review?templateURL=https://<YOUR_BUCKET>.s3.<REGION>.amazonaws.com/gip-templates/bedrock-auth-auth0.yaml&stackName=gip-auth-auth0
[l-google]: https://<REGION>.console.aws.amazon.com/cloudformation/home?region=<REGION>#/stacks/create/review?templateURL=https://<YOUR_BUCKET>.s3.<REGION>.amazonaws.com/gip-templates/bedrock-auth-google.yaml&stackName=gip-auth-google
[l-generic]: https://<REGION>.console.aws.amazon.com/cloudformation/home?region=<REGION>#/stacks/create/review?templateURL=https://<YOUR_BUCKET>.s3.<REGION>.amazonaws.com/gip-templates/bedrock-auth-generic.yaml&stackName=gip-auth-generic
[l-cognito-auth]: https://<REGION>.console.aws.amazon.com/cloudformation/home?region=<REGION>#/stacks/create/review?templateURL=https://<YOUR_BUCKET>.s3.<REGION>.amazonaws.com/gip-templates/bedrock-auth-cognito-pool.yaml&stackName=gip-auth-cognito-pool
[l-idc]: https://<REGION>.console.aws.amazon.com/cloudformation/home?region=<REGION>#/stacks/create/review?templateURL=https://<YOUR_BUCKET>.s3.<REGION>.amazonaws.com/gip-templates/bedrock-auth-idc.yaml&stackName=gip-auth-idc
[l-user-pool]: https://<REGION>.console.aws.amazon.com/cloudformation/home?region=<REGION>#/stacks/create/review?templateURL=https://<YOUR_BUCKET>.s3.<REGION>.amazonaws.com/gip-templates/cognito-user-pool-setup.yaml&stackName=gip-user-pool
[l-domain-cert]: https://us-east-1.console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/create/review?templateURL=https://<YOUR_BUCKET>.s3.us-east-1.amazonaws.com/gip-templates/cognito-custom-domain-cert.yaml&stackName=gip-cognito-domain-cert
[l-networking]: https://<REGION>.console.aws.amazon.com/cloudformation/home?region=<REGION>#/stacks/create/review?templateURL=https://<YOUR_BUCKET>.s3.<REGION>.amazonaws.com/gip-templates/networking.yaml&stackName=gip-networking
[l-otel]: https://<REGION>.console.aws.amazon.com/cloudformation/home?region=<REGION>#/stacks/create/review?templateURL=https://<YOUR_BUCKET>.s3.<REGION>.amazonaws.com/gip-templates/otel-collector.yaml&stackName=gip-monitoring
[l-dashboard]: https://<REGION>.console.aws.amazon.com/cloudformation/home?region=<REGION>#/stacks/create/review?templateURL=https://<YOUR_BUCKET>.s3.<REGION>.amazonaws.com/gip-templates/claude-code-dashboard.yaml&stackName=gip-dashboard
[l-cowork]: https://<REGION>.console.aws.amazon.com/cloudformation/home?region=<REGION>#/stacks/create/review?templateURL=https://<YOUR_BUCKET>.s3.<REGION>.amazonaws.com/gip-templates/cowork-dashboard.yaml&stackName=gip-cowork-dashboard
[l-queries]: https://<REGION>.console.aws.amazon.com/cloudformation/home?region=<REGION>#/stacks/create/review?templateURL=https://<YOUR_BUCKET>.s3.<REGION>.amazonaws.com/gip-templates/logs-insights-queries.yaml&stackName=gip-logs-insights
[l-analytics]: https://<REGION>.console.aws.amazon.com/cloudformation/home?region=<REGION>#/stacks/create/review?templateURL=https://<YOUR_BUCKET>.s3.<REGION>.amazonaws.com/gip-templates/analytics-pipeline.yaml&stackName=gip-analytics
[l-quota]: https://<REGION>.console.aws.amazon.com/cloudformation/home?region=<REGION>#/stacks/create/review?templateURL=https://<YOUR_BUCKET>.s3.<REGION>.amazonaws.com/gip-templates/quota-monitoring.yaml&stackName=gip-quota
[l-metering]: https://<REGION>.console.aws.amazon.com/cloudformation/home?region=<REGION>#/stacks/create/review?templateURL=https://<YOUR_BUCKET>.s3.<REGION>.amazonaws.com/gip-templates/quota-metering.yaml&stackName=gip-metering
[l-guardrails]: https://<REGION>.console.aws.amazon.com/cloudformation/home?region=<REGION>#/stacks/create/review?templateURL=https://<YOUR_BUCKET>.s3.<REGION>.amazonaws.com/gip-templates/guardrails-enforcement.yaml&stackName=gip-guardrails
[l-lifecycle]: https://<REGION>.console.aws.amazon.com/cloudformation/home?region=<REGION>#/stacks/create/review?templateURL=https://<YOUR_BUCKET>.s3.<REGION>.amazonaws.com/gip-templates/model-lifecycle.yaml&stackName=gip-model-lifecycle
[l-landing]: https://<REGION>.console.aws.amazon.com/cloudformation/home?region=<REGION>#/stacks/create/review?templateURL=https://<YOUR_BUCKET>.s3.<REGION>.amazonaws.com/gip-templates/landing-page-distribution.yaml&stackName=gip-landing-page
[l-skills]: https://<REGION>.console.aws.amazon.com/cloudformation/home?region=<REGION>#/stacks/create/review?templateURL=https://<YOUR_BUCKET>.s3.<REGION>.amazonaws.com/gip-templates/skills-registry.yaml&stackName=gip-skills-registry
[l-websearch]: https://us-east-1.console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/create/review?templateURL=https://<YOUR_BUCKET>.s3.us-east-1.amazonaws.com/gip-templates/bedrock-agentcore-gateway.yaml&stackName=gip-websearch-gateway
