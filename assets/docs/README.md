# User Documentation

This folder contains documentation for implementing and operating a governed AI inference platform on Amazon Bedrock — serving Claude Code, Claude Desktop (Cowork), and [any AWS-SDK harness](./HARNESSES.md) — with focus on the Enterprise Authentication deployment pattern.

## Deployment — Required Reading Order

Deployment has two sides: your **identity provider (IdP)** and **AWS infrastructure**. The IdP side must be completed first — the AWS wizard asks for values you get from your IdP.

### Step 1 — Configure your Identity Provider (do this first)

Choose your IdP and follow the setup guide before running `gip init`:

| Identity Provider | Guide |
|---|---|
| **Okta** | [okta-setup.md](./providers/okta-setup.md) |
| **Microsoft Entra ID (Azure AD)** | [microsoft-entra-id-setup.md](./providers/microsoft-entra-id-setup.md) |
| **Auth0** | [auth0-setup.md](./providers/auth0-setup.md) |
| **AWS Cognito User Pool** | [cognito-user-pool-setup.md](./providers/cognito-user-pool-setup.md) |
| **AWS IAM Identity Center (SSO)** | [iam-identity-center-setup.md](./providers/iam-identity-center-setup.md) |
| **Any other OIDC IdP** | [generic-oidc-setup.md](./providers/generic-oidc-setup.md) + the [IdP Requirements Contract](./providers/README.md) |

Not sure what your IdP must provide? The [IdP Requirements Contract](./providers/README.md) is the one-page spec of every claim the platform consumes and what breaks without it.

### Step 2 — Choose and deploy one path

Once you have your IdP **provider domain** and **client ID**, pick your path:

- **[DEPLOYMENT_PATHS.md](./DEPLOYMENT_PATHS.md)** — Six deployment paths mapped to customer profiles (visual console, wizard, GitOps answers file, by-hand CloudFormation console, apps gateway, agent-assisted) — the sole path chooser
- **[COMPONENTS.md](./COMPONENTS.md)** — What each module owns, requires, deploys, verifies, and cleans up
- **[QUICK_START.md](../../QUICK_START.md)** — Primary step-by-step reference for the wizard path
- **[AGENT_DEPLOY.md](./AGENT_DEPLOY.md)** — Playbook for deploying with a coding agent (Claude Code, OpenCode, etc.)
- **[DEPLOYMENT.md](./DEPLOYMENT.md)** — More conceptual/narrative walkthrough of the same steps
- **[MULTI_ACCOUNT.md](./MULTI_ACCOUNT.md)** — Scaling to multiple AWS accounts (per-Geo/LoB inference accounts, hub-and-spoke, SCP library, StackSets, payer cost rollup)
- **[NETWORK_ISOLATION.md](./NETWORK_ISOLATION.md)** — Private connectivity with VPC endpoints/PrivateLink, what can and cannot be made private, zero-trust conditions

### Reference

- **[CLI_REFERENCE.md](./CLI_REFERENCE.md)** — Complete `gip` command reference
- **[ARCHITECTURE.md](./ARCHITECTURE.md)** — Technical architecture details
- **[COMPONENTS.md](./COMPONENTS.md)** — Progressive-disclosure component map and adjacent dependencies
- **[LOCAL_TESTING.md](./LOCAL_TESTING.md)** — Testing before full deployment
- **[HARNESSES.md](./HARNESSES.md)** — Using other coding harnesses (OpenCode, Codex CLI, Pi, Aider) with the same credential-process, quota, and cost-attribution plumbing
- **[SKILLS_REGISTRY.md](./SKILLS_REGISTRY.md)** — Governed skill publishing, evals, and rollout through Agent Registry + S3 artifacts
- **[MEMORY.md](./MEMORY.md)** — Optional AgentCore Memory stack with server-derived actor IDs and deploy-gate posture
- **[WEB_SEARCH.md](./WEB_SEARCH.md)** — AgentCore web search gateway entitlement, audit, and metering posture
- **[GUARDRAILS.md](./GUARDRAILS.md)** — Optional account-level Amazon Bedrock Guardrails enforcement across configured Bedrock regions
- **[APPS_GATEWAY.md](./APPS_GATEWAY.md)** — Recommended Claude-apps architecture: Anthropic's self-hosted Claude apps gateway on ECS Fargate (server-side model access, managed settings, and spend caps; no per-developer AWS credentials)
- **[MCP_GATEWAY.md](./MCP_GATEWAY.md)** — How to extend the AgentCore MCP gateway from web search and memory to internal APIs/tools without trusting client-supplied identity
- **[adr/](./adr/README.md)** — Architecture Decision Records: why this fork's major decisions were made, with alternatives considered and evidence

## Operations

### Operational Runbooks

- **File**: [RUNBOOKS.md](./RUNBOOKS.md)
- **Purpose**: Step-by-step procedures for IdP secret rotation, OTEL collector outages, quota subsystem failures, version upgrades, and Claude Desktop service-token rotation
- **Audience**: IT administrators operating the deployed solution

### Failure Posture

- **File**: [FAILURE_POSTURE.md](./FAILURE_POSTURE.md)
- **Purpose**: Per-component failure matrix — what fails closed, what fails open, what degrades visibly during outages or degraded dependencies, and the honest gaps
- **Audience**: IT administrators and reviewers assessing operational readiness

### Monitoring Setup

- **File**: [MONITORING.md](./MONITORING.md)
- **Purpose**: CloudWatch monitoring configuration and OpenTelemetry setup
- **Audience**: IT administrators managing monitoring

### Analytics Pipeline

- **File**: [ANALYTICS.md](./ANALYTICS.md)
- **Purpose**: Setup and usage of the analytics pipeline for tracking Claude Code metrics
- **Audience**: IT administrators managing usage analytics

### Quota Management

- **File**: [QUOTA_MONITORING.md](./QUOTA_MONITORING.md)
- **Purpose**: Per-user and per-group token quota enforcement and alerts
- **Audience**: IT administrators managing usage costs

### SIEM Export

- **File**: [SIEM_EXPORT.md](./SIEM_EXPORT.md)
- **Purpose**: Exporting usage telemetry (metadata only) to Splunk, Datadog, or Elastic
- **Audience**: Security/observability teams integrating the platform with an existing SIEM

### Cost Planning Model

- **File**: [COST_ESTIMATES.md](./COST_ESTIMATES.md)
- **Purpose**: Scenario-based monthly estimates with topology floors, per-user usage, component add-ons, and runtime exclusions
- **Audience**: Operations and finance teams planning budget before deployment

### Cost Attribution

- **File**: [COST_ATTRIBUTION.md](./COST_ATTRIBUTION.md)
- **Purpose**: Per-user and per-team cost tracking via CUR 2.0 and Cost Explorer
- **Audience**: IT administrators and finance teams

## Claude Cowork (Desktop)

### CoWork 3P Guide

- **File**: [COWORK_3P.md](./COWORK_3P.md)
- **Purpose**: Using this solution's credential helper with Claude Desktop in third-party platform mode
- **Audience**: IT administrators deploying Claude Cowork with Amazon Bedrock
