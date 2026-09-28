# Quick Start Guide

Complete deployment walkthrough for IT administrators standing up the Governed Inference Platform on Amazon Bedrock **using the interactive wizard** (the credential-process path). One deployment powers internal products built on the AWS SDK, Claude Desktop (Cowork), and the CLI coding harnesses your developers choose — with SSO, budgets, attribution, and model governance enforced below the app layer.

> **Deploying Claude Code or Claude Desktop only?** The [Claude apps gateway](assets/docs/APPS_GATEWAY.md) is the recommended path — use the exact AWS Samples CDK pinned in `vendor/aws-samples/anthropic-on-aws/UPSTREAM.json` (run `scripts/fetch-claude-apps-gateway.sh` to materialize it). Use this wizard walkthrough when you need IAM Identity Center, CI usage, per-user CloudTrail/CUR attribution, internal AWS SDK apps, or other harnesses (OpenCode, Codex CLI, Pi, Aider).
> For GitOps/answers-file, fully by-hand CloudFormation console, or agent-assisted deployment, see [Choosing a Deployment Path](assets/docs/DEPLOYMENT_PATHS.md).

**Time Required:** 2-3 hours for initial deployment
**Skill Level:** AWS administrator with IAM/CloudFormation experience

---

## Prerequisites

### Software Requirements

- Python 3.10-3.13
- Poetry (dependency management)
- AWS CLI v2
- Git
- `unzip` on Linux (for presigned distribution ZIPs)
- Go 1.24+ for the default `gip package` build path; the deprecated `--legacy` path uses Python build tools instead

> **End users** need only Claude Code or Claude Desktop installed. No Python, Poetry, Go, or AWS account required — IT distributes pre-built packages.

**macOS admins — check your machine architecture before building:**

```bash
uname -m
```

| Output | Your Mac | Builds natively |
|--------|----------|----------------|
| `arm64` | Apple Silicon (M1/M2/M3/M4) | `macos-arm64` — Apple Silicon only; Intel Macs cannot run it |
| `x86_64` | Intel Mac | `macos-intel` — covers all Macs (runs natively on Intel, via Rosetta on Apple Silicon) |

> Intel (`macos-intel`) binaries run via Rosetta on Apple Silicon Macs, so a single Intel binary covers your entire Mac fleet. ARM64 binaries only run on Apple Silicon. If your admin is on Apple Silicon, use the [cross-arch setup](assets/docs/CLI_REFERENCE.md#cross-arch-macos-build-setup-optional) to build the Intel binary.

### AWS Requirements

- AWS account with appropriate IAM permissions to create:
  - CloudFormation stacks
  - IAM OIDC Providers or Cognito Identity Pools
  - IAM roles and policies
  - (Optional) Amazon Elastic Container Service (Amazon ECS) tasks and Amazon CloudWatch dashboards
  - (Optional) Amazon Athena, AWS Glue, AWS Lambda, and Amazon Data Firehose resources
  - (Optional) AWS CodeBuild
- Amazon Bedrock activated in target regions
- Access enabled for the Anthropic model or cross-region inference profile you will select in the Amazon Bedrock console

### OIDC Provider Requirements

This guide covers the **AWS infrastructure** side of the deployment. It assumes you have already configured your identity provider (IdP). **You must complete your IdP setup before running `gip init`** — the wizard will ask for your provider domain and client ID, and will fail without them.


| Your IdP | Setup guide |
|---|---|
| **Okta** | [Okta Setup Guide](assets/docs/providers/okta-setup.md) |
| **Microsoft Entra ID (Azure AD)** | [Microsoft Entra ID Setup Guide](assets/docs/providers/microsoft-entra-id-setup.md) |
| **Auth0** | [Auth0 Setup Guide](assets/docs/providers/auth0-setup.md) |
| **Google** | [Google Setup Guide](assets/docs/providers/google-oidc-setup.md) |
| **AWS Cognito User Pool** | [Cognito User Pool Setup Guide](assets/docs/providers/cognito-user-pool-setup.md) |
| **PingFederate, Keycloak, ForgeRock, or other generic OIDC** | [Generic OIDC Setup Guide](assets/docs/providers/generic-oidc-setup.md) |

Each guide walks through creating the application, setting the redirect URI to `http://localhost:8400/callback`, enabling PKCE, and noting the two values you will need here: your **provider domain** and **client ID**. For the full specification of what the platform consumes from tokens (which claims drive attribution, quotas, and dashboards — and what breaks without them), see the [IdP Requirements Contract](assets/docs/providers/README.md).

Once your IdP application is created and you have those two values, return here and continue from Step 1.



### Supported AWS Regions

The guidance can be deployed in any AWS region that supports:

- IAM OIDC Providers or Amazon Cognito Identity Pools
- Amazon Bedrock
- (Optional) Amazon Elastic Container Service (Amazon ECS) tasks and Amazon CloudWatch dashboards
- (Optional) Amazon Athena, AWS Glue, AWS Lambda, and Amazon Data Firehose resources
- (Optional) AWS CodeBuild

### Cross-Region Inference

Claude Code uses Amazon Bedrock's cross-region inference for optimal performance and availability. During setup, you can:

- Select your preferred Claude model (Opus, Sonnet, Haiku)
- Choose a cross-region profile (US, Europe, APAC) for optimal regional routing
- Select a specific source region within your profile for model inference

This automatically routes requests across multiple AWS regions to ensure the best response times and highest availability. Modern Claude models (3.7+) require cross-region inference for access.

---

## Deployment Steps

### Step 1: Clone Repository and Install Dependencies

```bash
# Clone the repository
git clone https://github.com/aws-samples/sample-guidance-for-governed-inference-platform-on-amazon-bedrock
cd sample-guidance-for-governed-inference-platform-on-amazon-bedrock/source

# Install dependencies
poetry install
```

### Step 2: Initialize Configuration

Run the interactive setup wizard:

```bash
poetry run gip init
```

The wizard runs through three numbered steps plus optional features. Every question is explained below — read this section before running the wizard so you know exactly what to enter.

> **Before you run `gip init`:** The wizard calls AWS APIs to validate account id (using your **administrator** credentials — not developer credentials). Make sure your terminal has a valid AWS session before you start. See [How gip init reads your AWS credentials](#how-gip-init-reads-your-aws-credentials) below.

The wizard collects:

- OIDC provider configuration (domain, client ID)
- AWS region selection for infrastructure
- Amazon Bedrock cross-region inference configuration
- Credential storage method (keyring or session files)
- Optional monitoring setup:
  - Enable monitoring? (yes/no)
  - Monitoring mode: **central collector** (ECS Fargate) or **sidecar collector** (local). Sidecar mode skips VPC configuration and Athena SQL pipeline setup (PromQL dashboards are included in both modes).
  - VPC configuration (central collector only)

---

#### Complete Wizard Flow — Decision Tree

Use this to quickly see which questions apply to your setup:

```
gip init
│
├── Profile name → e.g. "CorpIT-Prod"
│
├── STEP 1: Enable SSO authentication? (Y/n)
│   │
│   ├── Yes (default) ──────────────────────────────────────────────┐
│   │                                                                │
│   │   Provider domain? (e.g. company.okta.com)                    │
│   │   Client ID?                                                   │
│   │   │                                                            │
│   │   ├── Azure detected?                                          │
│   │   │   └── Auth mode: Public / Secret / Certificate             │
│   │   │       ├── Secret → enter client secret (stored in keyring) │
│   │   │       └── Certificate → cert path + key path               │
│   │   │                                                            │
│   │   ├── Credential storage: Keyring / Session Files              │
│   │   └── Federation type: Direct STS / Cognito Identity Pool      │
│   │                                                                │
│   └── No → skips all auth questions, goes to Step 2 ─────────────┘
│
├── STEP 2: AWS Infrastructure
│   ├── AWS region? (where CloudFormation stacks are deployed)
│   └── Stack base name? (prefix for all stack names)
│
├── OPTIONAL FEATURES
│   │
│   ├── Enable monitoring?
│   │   ├── No → skip to Windows builds
│   │   └── Yes
│   │       ├── VPC: Create new / Use existing
│   │       │   └── Existing → enter VPC ID + subnet IDs
│   │       ├── Enable HTTPS with custom domain?
│   │       │   ├── No → use HTTP (plain text endpoint)
│   │       │   └── Yes → domain name + Route53 hosted zone
│   │       ├── Enable analytics? (Athena + S3 data lake)
│   │       └── Enable quota monitoring?
│   │           └── Yes
│   │               ├── Monthly token limit (millions)
│   │               ├── Burst buffer % (5-25)
│   │               ├── Custom daily limit (optional)
│   │               ├── Daily enforcement: alert / block
│   │               ├── Monthly enforcement: alert / block
│   │               └── Quota re-check interval (minutes)
│   │
│   ├── Enable Windows builds? (CodeBuild)
│   ├── Generate CoWork 3P MDM config?
│   └── Distribution method?
│       ├── Presigned S3 URLs
│       ├── Authenticated Landing Page
│       │   ├── IdP provider + domain + client ID
│       │   ├── Custom domain (e.g. downloads.company.com)
│       │   └── Route53 hosted zone
│       └── Disabled
│
└── STEP 3: Bedrock Model Selection
    ├── Select Claude model (Sonnet / Haiku / Opus)
    ├── Cross-region inference profile (US / EU / APAC / Global)
    └── Source region (e.g. us-east-1)
```

---

#### Profile Name

**Q: `Enter a name for this profile:`**

The very first thing the wizard asks is a profile name. A **profile** is a named configuration set stored in `~/.gip/profiles/<name>.json`. It contains everything about one deployment: auth type, IdP domain, AWS region, stack names, monitoring settings, and model selection.

**Why profiles matter:**
- You run `gip init` once per deployment environment, not once per machine.
- Each profile maps to one set of AWS CloudFormation stacks.
- You can have multiple profiles on the same machine — for example `prod` and `staging`, or `us-prod` and `eu-prod` for regional deployments.

**Naming rules:** lowercase letters, numbers, and hyphens only. Good examples: `prod`, `corp-it-prod`, `us-bedrock-dev`.

**Profile commands:**
```bash
gip context list          # see all profiles
gip context use <name>    # switch active profile
gip context show          # view active profile details
```

Nothing is deployed to AWS when you run `gip init` — the profile is only saved locally. Deployment happens in Step 3.

---

#### How gip init reads your AWS credentials

`gip init` itself (the wizard running on your administrator machine) needs AWS credentials to call AWS APIs to validate that your account ID is reachable.

boto3 (the AWS SDK used internally) resolves credentials in this order — **first source that provides a valid, non-expired credential wins**:

| Priority | Source | How to set it |
|---|---|---|
| **1 — highest** | Environment variables | `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` (+ optional `AWS_SESSION_TOKEN`) |
| **2** | `~/.aws/credentials` file | `[default]` or named profile via `AWS_PROFILE` |
| **3** | `~/.aws/config` file | SSO profiles (`aws sso login`), `credential_process` entries, assumed-role profiles |
| **4** | IAM instance profile | Automatic on EC2 — no config needed |
| **5 — lowest** | ECS container role | Automatic on ECS tasks — no config needed |

**Common issue: stale environment variables override everything.** If you ran `aws sts assume-role` earlier and those vars are still exported, boto3 will try them first — even if they are expired — and will not fall back to your credential file. If `gip init` fails with an AWS credentials error but `aws sts get-caller-identity` works from a fresh shell, see [Troubleshooting](#gip-init-fails-aws-credentials-configured-even-though-aws-sts-get-caller-identity-works) below.

**Recommended for most administrators:**
```bash
# SSO login (if your org uses IAM Identity Center)
aws sso login --profile <your-admin-profile>
export AWS_PROFILE=<your-admin-profile>

# Verify before starting
aws sts get-caller-identity

# Then run the wizard
poetry run gip init
```

---

#### Step 1: Authentication Configuration

**What it asks:** `Enable SSO authentication? (Y/n)`

Choose whether developers will authenticate through an OIDC identity provider to reach Bedrock:

| Answer | When to use |
|---|---|
| **Yes** (default) | You have Okta, Azure AD, Auth0, or Cognito User Pool — full per-user attribution and quota enforcement |
| **No** | Analytics-only deployment, or developers already have IAM/role access to Bedrock |

> **Note:** AWS IAM Identity Center is fully supported — select **AWS IAM Identity Center** in the wizard's authentication step. Identity comes from the `AWSReservedSSO_*` role session name and quota enforcement works via SigV4. See the [IAM Identity Center setup guide](assets/docs/providers/iam-identity-center-setup.md).

---

##### If you answered Yes (SSO enabled)

**Q: `Enter your OIDC provider domain:`**

Enter the domain of your identity provider — the base URL without `https://`:

| Provider | Example value |
|---|---|
| Okta | `company.okta.com` |
| Microsoft Entra ID | `login.microsoftonline.com/{your-tenant-id}/v2.0` |
| Auth0 | `company.auth0.com` |
| Cognito User Pool | `my-app.auth.us-east-1.amazoncognito.com` |

The wizard auto-detects the provider type from the domain for the four known providers above. If it detects Cognito, it will also ask for your **User Pool ID** (case-sensitive, format: `us-east-1_XXXXXXXXX`).

> **Custom or non-standard OIDC domains** (e.g. Keycloak, PingFederate, Okta vanity domains like `sso.mycompany.com`): the wizard cannot auto-detect the type and will prompt you to select manually:
> ```
> Could not auto-detect provider type from domain.
> Select your identity provider type:
>   > Okta (or generic OIDC)
>     Microsoft Entra ID / Azure AD
>     Auth0
>     AWS Cognito User Pool
> ```
> Choose **Okta (or generic OIDC)** for any standard OIDC provider not listed (Keycloak, PingFederate, ADFS, etc.) — it uses the most compatible CloudFormation template.

---

**Q: `Enter your OIDC Client ID:`**

The Application (client) ID from your IdP app registration. You noted this during the IdP setup in Step 0.

- Okta: found in Applications → your app → General tab
- Azure: found in App registrations → your app → Overview → Application (client) ID
- Auth0: found in Applications → your app → Settings → Client ID

---

**Q (Azure only): `Select authentication mode:`**

Only shown for Azure AD / Entra ID. Choose based on whether your tenant allows public client flows:

| Mode | When to use |
|---|---|
| **Public client** | Personal tenant or `Allow public client flows = Yes` in your app — simplest option, no secret needed |
| **Confidential — client secret** | Enterprise tenant with `Allow public client flows = No` — uses a shared app secret |
| **Confidential — certificate** | Enterprise tenant, production recommended — uses a certificate/key pair, no shared secret |

> Check in Azure portal: App registration → Authentication → Advanced settings → "Allow public client flows"

If you choose **certificate mode**, the wizard will ask for two file paths:
- `Path to certificate PEM file:` — enter `~/gip/cert.pem` (works on all platforms)
- `Path to private key PEM file:` — enter `~/gip/key.pem`

Use `~/` relative paths — they resolve correctly on macOS, Linux and Windows. The cert files must exist at those paths on every user machine. See [Certificate Setup](assets/docs/providers/microsoft-entra-id-setup.md#5-confidential-client-setup-enterprise) for how to generate and distribute them.

---

**Q: `Select credential storage method:`**

Choose how the `credential-process` binary stores AWS temporary credentials on the user's machine:

| Option | What it does | When to use |
|---|---|---|
| **Keyring** | OS secure storage (macOS Keychain, Windows Credential Manager, Linux Secret Service) | Production, and recommended for CoWork 3P |
| **Session Files** | Temp files in `~/.aws/credentials` and `~/.gip-session/` | Dev/testing — simpler, wiped on logout |

Default is **Session Files**. Both modes work for Claude Code CLI. For **CoWork Desktop 3P**, Keyring is strongly recommended: CoWork resolves credentials through `inferenceBedrockProfile` → boto3's named-profile resolution, and boto3 reads `~/.aws/credentials` before the `credential_process` entry in `~/.aws/config`. In Session Files mode, that means boto3 uses whatever static credentials the last CLI invocation wrote to the file and will **not** auto-refresh them through `credential_process` once they expire — CoWork fails with `403 The security token included in the request is invalid` until the CLI is run again to repopulate the file. Keyring mode keeps `~/.aws/credentials` untouched, so boto3 falls through to `credential_process` and the binary handles refresh transparently. Keyring may show a one-time OS permission prompt on first use.

---

**Q: `Choose federation type:`**

How the OIDC token is exchanged for AWS temporary credentials:

| Option | How it works | Max session | When to use |
|---|---|---|---|
| **Direct STS** | OIDC token → STS `AssumeRoleWithWebIdentity` → temp creds | 12 hours | Recommended for most deployments — simpler, longer sessions |
| **Cognito Identity Pool** | OIDC token → Cognito Identity Pool → temp creds | 1 hour (enhanced flow) | When you need Cognito features like principal tag mapping |

**Default: Direct STS.** Unless you have a specific reason for Cognito, use Direct STS.

---

##### If you answered No (SSO disabled)

No authentication questions are asked. The wizard skips directly to Step 2.

**What SSO disabled means in practice:**

- **No auth infrastructure is deployed** — no IAM OIDC Provider, no Cognito Identity Pool, no IAM role for developers is created.
- **No `credential_process` binary is distributed** — end users will not get an installer or auto-refreshing AWS credentials from this tool.
- **You are responsible for giving developers Bedrock access** via whatever IAM mechanism already exists in your account (IAM users, existing roles, existing SSO, etc.).

**When to choose No:**

| Scenario | Why disabling SSO makes sense |
|---|---|
| You only want the monitoring/analytics stack | Deploy dashboards without changing how developers authenticate |
| Developers already have Bedrock access via existing roles | Adding another auth layer would be redundant |
| Pilot/testing with a shared IAM user | Fastest way to test the monitoring stack before committing to full OIDC setup |
| You will configure auth manually after deployment | Advanced users who want to customise the CloudFormation templates directly |

> **Note:** Quota monitoring and per-user attribution require SSO enabled. With SSO disabled, the monitoring stack still collects aggregate metrics but cannot attribute usage to individual users.

---

#### Step 2: AWS Infrastructure Configuration

**Q: `Select AWS Region for infrastructure deployment:`**

The region where CloudFormation will create authentication resources (IAM OIDC Provider or Cognito Identity Pool, IAM roles, monitoring stack if enabled). This does **not** have to match the region where Bedrock is invoked — you configure Bedrock regions separately in Step 3.

Choose the region closest to your team or where your compliance requirements dictate resources must reside.

---

**Q: `Stack base name:` (Direct STS) or `Identity Pool Name:` (Cognito)**

A name prefix used for all CloudFormation stack names created by this deployment. Example: `gip-auth` produces:
- `gip-auth-stack` — main auth stack
- `gip-auth-monitoring` — OTEL collector (if enabled)
- `gip-auth-dashboard` — CloudWatch dashboard (if enabled)
- `gip-auth-analytics` — Athena pipeline (if enabled)

Use lowercase letters, numbers and hyphens only. Must be unique within your AWS account/region.

---

#### Optional Features

---

##### Monitoring and Usage Dashboards

**Q: `Enable monitoring?`**

Deploys an OpenTelemetry collector on ECS Fargate + CloudWatch dashboard showing per-user token usage, costs, model breakdown, and quota status.

- **Yes** → continues to VPC and HTTPS configuration below
- **No** → skips all monitoring questions; auth infrastructure only

> **Important:** The wizard asks for an ALB scheme: **internet-facing** (default) or **internal** (private — reachable only via VPN/Direct Connect/internal network). If your VPC has no Internet Gateway (fully private environment), choose **internal** — an internet-facing ALB cannot be provisioned there.

---

**Q: VPC Configuration** (shown if monitoring = Yes)

The wizard asks whether to create a new VPC or use an existing one:

- **Create new VPC** — wizard creates a VPC with public/private subnets automatically. Simplest option.
- **Use existing VPC** — you provide your VPC ID and at least 2 subnet IDs. Use this if you have networking requirements (VPC peering, PrivateLink, specific CIDR ranges).

> An **internet-facing** ALB requires your VPC to have an Internet Gateway. In fully private environments, choose the **internal** ALB scheme instead.

---

**Q: `Enable HTTPS with custom domain?`**

| Answer | What happens |
|---|---|
| **Yes** (required for production and internet-facing deployments) | Provide a custom domain (e.g. `telemetry.company.com`) and Route53 hosted zone. CloudFormation creates an ACM certificate and DNS record automatically. |
| **No** | Central monitoring stops unless you chose an internal ALB and explicitly accept the local-development-only insecure mode. That mode sends telemetry and bearer tokens over plaintext HTTP and must never be used in production. |

If you answer **Yes**, the wizard asks:
- `Enter custom domain name:` — e.g. `telemetry.company.com`
- `Select Route53 hosted zone:` — the wizard lists zones in your account; select the one that matches your domain

> If DNS is managed outside Route53, set `certificate_arn` in the profile's
> `monitoring_config`. The certificate must be in ACM and cover `custom_domain`.

**Migration from earlier releases:** central collector stacks that omitted a
custom domain previously defaulted to plaintext HTTP. Before the next update,
configure `monitoring_config.custom_domain` plus `hosted_zone_id` or
`certificate_arn`. For temporary internal local development only, set
`monitoring_config.alb_scheme` to `internal` and
`monitoring_config.allow_insecure_http_ingress` to `true`. Remove that exception
after TLS is configured. Internet-facing plaintext ingress is rejected by both
the CLI and CloudFormation.

---

##### Analytics Pipeline

**Q: `Enable analytics?`**

Deploys Kinesis Data Firehose → S3 data lake → Athena with 10 pre-built SQL queries for historical token usage analysis.

- Additional cost: ~$5/month for light usage
- Gives you 90-day hot storage + Glacier archival
- Useful for chargeback, cost attribution by team/department, trend analysis

You can enable this later by re-running `gip init` and `gip deploy analytics`.

---

##### Quota Monitoring

**Q: `Enable quota monitoring?`**

Enforces per-user monthly and daily token limits. Sends SNS alerts at 80%, 90%, and 100% of limits. Can block credential issuance when limits are exceeded.

If **Yes**, the wizard asks:

**Q: `Monthly token limit per user (in millions):`**
Default: `225` (= 225,000,000 tokens/month). Adjust based on your team's expected usage.

**Q: `Burst buffer percentage (5-25%):`**
Daily limit = (monthly ÷ 30) × (1 + buffer%). The buffer allows for legitimate heavy days above the average without triggering alerts.
- `5%` = strict, blocks heavy days quickly
- `10%` = default, balanced
- `25%` = flexible, only catches extreme spikes

**Q: `Custom daily limit:`**
Press Enter to accept the calculated value, or enter a specific number.

**Q: `Daily limit enforcement:` and `Monthly limit enforcement:`**

| Mode | Behaviour |
|---|---|
| **alert** | Send SNS notification, allow continued use |
| **block** | Deny credential issuance when limit exceeded |

Recommended defaults: Daily = **alert**, Monthly = **block**

**Q: `Quota check interval (minutes):`**
How often quota is re-checked when credentials are cached.
- `0` = check every request (adds ~200ms latency, strictest enforcement)
- `30` = every 30 minutes (default — good balance)
- `60` = hourly (minimal impact, 1-hour enforcement gap)

---

##### Windows Build Support

**Q: `Enable Windows builds?`**

Deploys an AWS CodeBuild project to compile the Windows `.exe` binary using Nuitka. Windows builds take ~20 minutes and run in the cloud — you don't need a Windows machine.

- Answer **Yes** if you have Windows users
- Answer **No** to skip — you can enable it later by re-running `gip init`

---

##### Claude Cowork (Desktop) Support

**Q: `Generate CoWork 3P MDM configuration during packaging?`**

When **Yes**, every `gip package` run automatically produces MDM configuration files alongside the standard installer. These deploy Claude Desktop (Claude Cowork) pointing at Bedrock through the same credential infrastructure. No extra AWS resources required.

Output files in `dist/cowork-3p/`:
- `cowork-3p.mobileconfig` — deploy via Jamf/Kandji/Mosyle (macOS). Unsigned profiles cannot auto-install: after delivery (or when `install.sh` runs), the user must approve the profile in **System Settings → Privacy & Security → Profiles**.
- `cowork-3p.reg` — deploy via Intune/Group Policy (Windows). Writes to `HKCU\SOFTWARE\Policies\Claude` (per-user, no admin elevation); do not redirect to `HKLM`.
- `cowork-3p-config.json` — raw MDM JSON for the Claude Desktop Setup UI / manual review

Claude Desktop authenticates via the `inferenceBedrockProfile` MDM key, which points at the AWS named profile that `install.sh` / `install.bat` writes to `~/.aws/config`. No per-user wrapper script is required. Users must run the installer **before** opening Claude Desktop — otherwise the named profile won't exist and Bedrock mode won't activate.

See [COWORK_3P.md](assets/docs/COWORK_3P.md) for MDM deployment instructions.

---

##### Package Distribution

**Q: `Distribution method:`**

How to deliver the installer package to end users:

| Option | How it works | Best for |
|---|---|---|
| **Presigned S3 URLs** | `gip distribute` uploads to S3 and generates a time-limited link (48h default) you share via Slack/email | Any team size, no extra infrastructure |
| **Authenticated Landing Page** | Self-service web portal — users log in with SSO and download the right binary for their OS | Large orgs needing compliance, audit trail, self-service |
| **Disabled** | You distribute the `dist/` folder manually (zip + email, shared drive, artifact repo) | Simple pilots, internal testing |

If you choose **Landing Page**, the wizard asks for:
- IdP provider for the web portal (can be different from your developer IdP)
- Custom domain for the download portal (e.g. `downloads.company.com`)
- Route53 hosted zone

---

#### Step 3: Bedrock Model Selection

**Q: `Select Claude model:`**

The default model developers will use. This sets `ANTHROPIC_MODEL` in the distributed `settings.json`.

| Model | Cost | Best for |
|---|---|---|
| **Claude Sonnet** | Mid | Most development tasks — best balance of speed and capability |
| **Claude Haiku** | Lowest | High-volume, fast tasks — autocomplete, simple edits |
| **Claude Opus** | Highest | Complex reasoning, architecture, hard problems |

**Q: `Select cross-region inference profile:`**

Routes Bedrock requests across multiple AWS regions within a geography for higher availability and throughput. All regions within a profile have the same pricing.

| Profile | Routes within | Required for Claude 3.7+ |
|---|---|---|
| **US** (`us.`) | US East, US West | Yes — Claude 3.7+ only available via cross-region |
| **EU** (`eu.`) | EU regions | For EU data residency compliance |
| **APAC** (`ap.`) | Asia Pacific regions | For APAC deployments |
| **Global** (`global.`) | All regions worldwide | Maximum throughput |

> **Important:** Claude models 3.7 and newer require cross-region inference. Direct single-region invocation is only available for older models.

**Q: `Select source region:`**

The AWS region where Bedrock API calls originate. Choose the region closest to your developers or your primary AWS region. Requests may be routed to other regions within the profile for capacity, but billing and data residency are anchored to the selected geography.

---

#### What `gip init` saves

When the wizard completes, configuration is saved to `~/.gip/profiles/<name>.json` on your machine (one file per profile). A `~/.gip/config.json` file tracks which profile is currently active.

**Nothing is deployed to AWS at this point.** The wizard only writes local config. Deployment happens in Step 3.

If you need to re-run the wizard to change settings, run `gip init` again with the same profile name — it will overwrite the saved profile. If you want to add a second deployment environment, run `gip init` again with a new profile name.

---

### Step 3: Deploy Infrastructure

Deploy the AWS CloudFormation stacks:

```bash
poetry run gip deploy
```

This deploys in order based on what you configured in Step 2:

**Auth stack** (always deployed):

| Resource | What it does |
|---|---|
| IAM OIDC Provider (Direct STS) or Cognito Identity Pool | Trusts your IdP — validates OIDC tokens from Okta/Azure/Auth0 |
| IAM Role with `bedrock:InvokeModel` | What developers assume after OIDC login — scoped to Bedrock only |
| IAM trust policy | Allows only tokens from your specific IdP client ID to assume the role |

**Monitoring stack** (if monitoring = Yes):

- VPC and networking resources (or integration with existing VPC)
- ECS Fargate cluster running OpenTelemetry collector
- Application Load Balancer for OTLP ingestion
- CloudWatch Log Groups and Metrics
- CloudWatch Dashboard with PromQL widgets (no Lambda functions)
- Kinesis Data Firehose for streaming metrics to S3 (if analytics enabled)
- Amazon Athena for SQL analytics on collected metrics (if analytics enabled)
- S3 bucket for long-term metrics storage (if analytics enabled)

**Quota stack** (if quota monitoring = Yes):

| Resource | What it does |
|---|---|
| DynamoDB table (`QuotaPolicies`) | Stores per-user/group/default token limits |
| Lambda (quota-monitor) | Runs every 15 min — checks thresholds via PromQL, sends alerts |
| SNS topic | Delivers quota alerts to subscribed email/webhook |
| API Gateway (quota check) | Real-time quota check at credential issuance time |

**CodeBuild stack** (if Windows builds = Yes):

| Resource | What it does |
|---|---|
| CodeBuild project | Compiles Windows `.exe` using Nuitka (~20 min per build) |
| S3 bucket | Stores compiled Windows binaries |

**Deployment takes 5–15 minutes** depending on which stacks are enabled. Monitor progress:

```bash
poetry run gip status
```

### Step 4: Create Distribution Package

Build the package for end users:

```bash
# On a macOS admin host, build all platforms
poetry run gip package --target-platform all

# Creates ready-to-distribute packages for:
# - macOS ARM64 (Apple Silicon) and Intel
# - Linux x64 and ARM64
# - Windows x64
# macOS targets require macOS because Keychain support uses CGO. Requires Go 1.24+.
```

> **Note:** Bare `gip package` uses local Go cross-compilation and requires Go 1.24+. Use `--legacy` only for the deprecated PyInstaller/CodeBuild compatibility path.

**Choosing macOS targets:**

Before selecting, check your machine's architecture:

```bash
uname -m
python3 -c "import platform; print(platform.machine())"
poetry run python -c "import platform; print(platform.machine())"
```

All three should return the same value. The Poetry command is most important — it confirms what architecture PyInstaller will use when building the binary.

- `arm64` → you are on Apple Silicon — select `macos-arm64`
- `x86_64` → you are on Intel — select `macos-intel`

The `gip package` command prompts you to select one or more platforms via a checkbox. **You must build for the architecture your developers are running** — ask your developers to run the same commands on their machines and tell you the output before you build:

```bash
uname -m
python3 -c "import platform; print(platform.machine())"
```

Pick based on what your developers report:

| Your developers report | Select |
|------------------------|--------|
| `arm64` (Apple Silicon) | `macos-arm64` |
| `x86_64` (Intel) | `macos-intel` |
| Both | `macos-arm64` + `macos-intel` |

> **Note:** `macos-intel` binaries run on all Macs — natively on Intel, via Rosetta on Apple Silicon. The default Go path builds both macOS architectures on a macOS host. Universal2 Python is required only for a cross-architecture `--legacy` build.

**Package Workflow:**

1. **Cross-compile locally**: Go builds Linux and Windows targets from any supported administrator OS. macOS targets require a macOS host with Apple build tools because Keychain support uses CGO.
2. **Create distribution**: Run `poetry run gip distribute` to upload the completed package and generate presigned URLs.

The deprecated `--legacy` path retains the host-specific PyInstaller and optional Windows CodeBuild constraints. See [CLI Reference](assets/docs/CLI_REFERENCE.md#package-create-distribution) if temporary legacy compatibility is required.

The `dist/` folder will contain:

- `credential-process-macos-arm64` - Authentication executable for macOS ARM64
- `credential-process-macos-intel` - Authentication executable for macOS Intel (if built)
- `credential-process-windows.exe` - Authentication executable for Windows
- `credential-process-linux-x64` and `credential-process-linux-arm64` - Authentication executables for Linux
- `config.json` - Embedded configuration
- `install.sh` - Installation script for Unix systems
- `install.bat` - Installation script for Windows (launcher)
- `gip-install.ps1` - PowerShell installer logic (called by install.bat)
- `README.md` - User instructions
- `.claude/settings.json` - Claude Code telemetry settings (if monitoring enabled)
- `otel-helper-*` - OTEL helper executables for each platform (if monitoring enabled)

The package builder:

- Builds macOS, Linux, and Windows targets on a macOS host; Linux and Windows targets on Linux or Windows hosts
- Fails the package command if any requested credential or monitoring binary cannot be built, rather than publishing an incomplete package
- Refuses to attempt macOS targets from a non-macOS host (fails fast with a clear error rather than silently producing invalid binaries)
- Includes the OTEL helper for extracting user attributes from JWT tokens
- Creates a unified installer that auto-detects the user's platform

### Step 5: Test the Setup

Verify everything works correctly:

```bash
poetry run gip test
```

This will:

- Simulate the end-user installation process
- Test OIDC authentication
- Verify AWS credential retrieval
- Check Amazon Bedrock access
- (Optional) Test actual API calls with `--api` flag

### Step 6: Distribute Packages to Users

You have three options for sharing packages with users. The distribution method is configured during `gip init` (Step 2).

#### Option 1: Manual Sharing

No additional infrastructure required. Share the built packages directly:

```bash
# Navigate to dist directory
cd dist

# Create a zip file of all packages
zip -r gip-packages.zip .

# Share via email or internal file sharing
# Users extract and run install.sh (Unix) or install.bat (Windows)
```

**Best for:** Any size team, no automation required

#### Option 2: Presigned S3 URLs

Automated distribution via time-limited S3 URLs:

```bash
poetry run gip distribute
```

Generates presigned URLs (default 48-hour expiry) that you share with users via email or messaging.

**Best for:** Automated distribution without authentication requirements

**Setup:** Select "presigned-s3" distribution type during `gip init` (Step 2)

#### Option 3: Authenticated Landing Page

Self-service portal with IdP authentication:

```bash
# Deploy landing page infrastructure (if not done during Step 3)
poetry run gip deploy distribution

# Upload packages to landing page
poetry run gip distribute
```

Users visit your landing page URL, authenticate with SSO, and download packages for their platform.

**Best for:** Self-service portal with compliance and audit requirements

**Setup:** Select "landing-page" distribution type during `gip init` (Step 2), then deploy distribution infrastructure

See [Distribution Comparison](assets/docs/distribution/comparison.md) for detailed feature comparison and setup guides.

---

## Platform Builds

### Build Requirements

- **Go 1.24+**: Required for the default package path. Linux and Windows targets cross-compile from any supported administrator host.
- **macOS targets**: Require a macOS host with Apple build tools because the credential process uses CGO for Keychain access.
- **Default target set**: macOS hosts build macOS ARM64/Intel, Linux x64/ARM64, and Windows; Linux and Windows hosts build Linux x64/ARM64 and Windows.
- **Legacy path**: `gip package --legacy` uses PyInstaller and optional Windows CodeBuild. Its Docker and universal2-Python constraints are documented in the [CLI Reference](assets/docs/CLI_REFERENCE.md#package-create-distribution).

### Optional: Legacy Cross-Arch macOS Builds

The default Go path builds both macOS architectures on a macOS host. Universal2 Python is needed only for the deprecated `--legacy` path; see [CLI Reference - Cross-arch macOS Build Setup](assets/docs/CLI_REFERENCE.md#cross-arch-macos-build-setup-optional).

---

## Cleanup

You are responsible for the costs of AWS services while running this guidance. If you decide that you no longer need the guidance, please ensure that infrastructure resources are removed.

```bash
poetry run gip destroy
```

---

## Troubleshooting

### `gip init` fails "AWS credentials configured" even though `aws sts get-caller-identity` works

This is almost always caused by **expired AWS environment variables** overriding your credential file. boto3 (used internally by `gip`) resolves credentials in a strict priority order and stops at the first source that provides values — even if those values are expired:

```
1. AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY env vars   ← highest priority
2. ~/.aws/credentials file
3. ~/.aws/config file (SSO, credential_process, assumed roles)
4. IAM instance profile (EC2 only)
5. ECS container role                                   ← lowest priority
```

If `AWS_ACCESS_KEY_ID` is set in your environment but expired, boto3 will **not** fall back to `~/.aws/credentials`. It will simply fail. This is the most common cause of this error.

**Fix:**

```bash
# 1. Check what is set
env | grep AWS_

# 2. Unset any stale values
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN

# 3. Verify boto3 now resolves credentials correctly
python3 -c "import boto3; print(boto3.client('sts').get_caller_identity())"

# 4. Re-run init
poetry run gip init
```

If you are using `aws sso login`, make sure the SSO session is active before running `gip init`:

```bash
aws sso login --profile <your-profile>
export AWS_PROFILE=<your-profile>
poetry run gip init
```

### Authentication Issues (end-user credential refresh)

Force re-authentication after deployment:

```bash
~/gip/credential-process --clear-cache
```

If CoWork Desktop 3P fails with `403 The security token included in the request is invalid` and the user was previously on a session-files build, `~/.aws/credentials` may contain a stale `[<profile-name>]` stanza with literal `EXPIRED` values that shadows the current `credential_process` entry in `~/.aws/config`. Re-run `install.sh` / `install.bat` — it purges any such stanza before writing the new profile. Alternatively, remove the block by hand.

### Port Configuration

The credential provider uses port 8400 by default for OAuth callbacks. This port also serves as an inter-process lock: if multiple credential-process invocations run concurrently, the second will wait for the first to complete authentication and then read credentials from cache.

**Important:** The callback port must match the redirect URI registered in your IdP application (e.g., `http://localhost:8400/callback`). If port 8400 is occupied by another application on your users' machines (e.g., Commvault, HashiCorp Vault), configure a different port.

**Option 1: Configure in profile** (recommended — persisted in config.json):

During `gip init`, select "Use a custom OAuth callback port" when prompted. Or manually add to `config.json`:

```json
{
  "ProfileName": {
    "redirect_port": 8401,
    ...
  }
}
```

**Option 2: Environment variable** (takes precedence over config.json):

```bash
export REDIRECT_PORT=8401
```

Whichever port you choose, ensure `http://localhost:<port>/callback` is registered as a valid redirect URI in your IdP application configuration.

### `Exec format error` on the credential-process binary (end user)

If an end user sees this when running `aws sts get-caller-identity` or launching Claude:

```
[Errno 8] Exec format error: '/Users/<username>/gip/credential-process'
```

or directly:

```
zsh: exec format error: ./credential-process
```

**This is a CPU architecture mismatch** — the binary was built for a different architecture than the user's machine. `chmod +x` will not fix it.

**Diagnose (run on the user's machine):**

```bash
uname -m                                                  # their CPU arch
file ~/gip/credential-process        # binary's CPU arch
```

| `uname -m` result | Binary arch | Cause |
|---|---|---|
| `x86_64` (Intel Mac) | `arm64` | Intel binary was not built — only ARM64 was in the package |
| `arm64` (Apple Silicon) | `x86_64` | Wrong binary manually copied |

**Fix (admin) — rebuild with both macOS architectures:**

```bash
# One-time setup: install Python universal2 from https://www.python.org/downloads/macos/
# Download the "macOS 64-bit universal2 installer" for Python 3.12 and run it.
# gip detects it automatically at /Library/Frameworks/Python.framework/

# Rebuild — now produces both macos-arm64 and macos-intel
poetry run gip package --target-platform all
```

Redistribute the new package. The installer auto-detects architecture and installs the correct binary.

> **Why this happens:** Without a universal2 Python, `gip package` builds only for the host Mac's architecture. An ARM64-only package has no Intel binary, so Intel Mac users get `exec format error` — ARM64 binaries cannot run on Intel Macs. Install a universal2 Python to also build the Intel binary, which covers all Mac users.

### Windows `install.bat` — `-replace was unexpected at this time.`

If running `install.bat` on Windows produces this error:

```
-replace was unexpected at this time.
```

**Root cause:** This is a cmd.exe parser bug in the generated installer — `^` line-continuation characters inside a double-quoted PowerShell command get consumed by cmd.exe, causing `-replace` to be treated as a standalone batch command rather than part of the PowerShell string. A code fix is included in the next release.

**Workaround:** The binary and `config.json` are already copied before this error occurs — only the `~/.claude/settings.json` placeholder replacement fails. Complete the installation manually:

**Step 1** — Open **PowerShell** (not cmd.exe) from the extracted package folder and run:

```powershell
$otelPath = "$env:USERPROFILE\gip\otel-helper.exe" -replace '\\', '/'
$credPath = "$env:USERPROFILE\gip\credential-process.exe" -replace '\\', '/'
(Get-Content 'gip-settings\settings.json') `
    -replace '__OTEL_HELPER_PATH__', $otelPath `
    -replace '__CREDENTIAL_PROCESS_PATH__', $credPath |
    Set-Content "$env:USERPROFILE\.claude\settings.json"
```

**Step 2** — Configure the AWS profile (replace `<profile-name>` with the name shown in `config.json`):

```powershell
aws configure set credential_process `
    "$env:USERPROFILE\gip\credential-process.exe --profile <profile-name>" `
    --profile <profile-name>
```

> **Why PowerShell works:** PowerShell uses backtick (`` ` ``) for line continuation — there is no cmd.exe parser involved to mangle the `-replace` operators.

### Build Failures

Check Windows build status:

```bash
poetry run gip builds
```

### Stack Deployment Issues

View stack status:

```bash
poetry run gip status
```

For detailed troubleshooting, see [Deployment Guide](assets/docs/DEPLOYMENT.md).

---

## Next Steps

- [Architecture Deep Dive](assets/docs/ARCHITECTURE.md) - Technical architecture details
- [Enable Monitoring](assets/docs/MONITORING.md) - Setup OpenTelemetry monitoring
- [Setup Analytics](assets/docs/ANALYTICS.md) - Configure S3 data lake and Athena queries
- [CLI Reference](assets/docs/CLI_REFERENCE.md) - Complete command reference
