# Choosing a Deployment Path

There are six ways to stand up the Governed Inference Platform. They all
produce the same governed inference endpoint — corporate SSO in front of
Amazon Bedrock with per-user budgets, attribution, and model guardrails —
powering internal products built on the AWS SDK, SuperApps like Claude
Desktop (Cowork), and CLI coding harnesses alike. But they suit different
teams. Pick the row that sounds like your organization, then follow that
path's guide.

> **Deploying Claude Code / Claude Desktop only?** Start with **Path 4 (Claude
> apps gateway)** — Anthropic's first-party control plane, recommended for
> Claude apps on Bedrock. Paths 1–3 deploy the credential-process
> architecture; choose them when you need IAM Identity Center, CI usage,
> per-user CloudTrail/CUR attribution, or non-Claude harnesses.

| # | Path | Best for | Admin skill assumed | Time to first token |
|---|------|----------|--------------------|---------------------|
| 0 | [Visual console](#path-0-visual-console-recommended-first-run) | First deployment; admins who prefer a guided GUI over a terminal wizard | AWS basics, a browser | ~2–3 h |
| 1 | [Interactive wizard](#path-1-interactive-wizard-credential-process-default) | Most platform teams deploying the credential-process architecture | CLI comfort, AWS basics | ~2–3 h |
| 2 | [GitOps / answers file](#path-2-gitops-answers-file) | Orgs with CI/CD change control | Pipeline authoring | ~2–3 h first time, minutes after |
| 3 | [By hand (CloudFormation console)](#path-3-by-hand-cloudformation-console) | Traditional/change-managed businesses; no Python on admin machines | AWS console, change tickets | ~half a day |
| 4 | [Claude apps gateway](#path-4-claude-apps-gateway-recommended-for-claude-apps) | **Recommended for Claude Code / Claude Desktop** — one central control plane, no per-dev AWS credentials | Pinned AWS Samples CDK (fetched) + image build | Follow upstream estimate |
| 5 | [Agent-assisted](#path-5-agent-assisted-deployment) | Teams already using Claude Code / another coding agent | Supervising an agent | ~1 h supervised |

Two cross-cutting facts to know before choosing:

- **Every path needs the same two manual prerequisites** no tool can do for
  you: (a) register an OIDC application in your identity provider
  ([provider guides](providers/README.md)), and (b) enable Anthropic model access in
  the Amazon Bedrock console for your regions.
- **End users never need Python, AWS accounts, or build tools** on any path —
  they receive a package (or MDM profile) and sign in with their normal
  work account.
- **Region choice is a separate architecture decision.** Review the
  [eight-region deployability and residency matrix](ARCHITECTURE.md#eight-region-deployability)
  before selecting an infrastructure Region, CRIS geography, or optional
  content service.

---

## Path 0 — Visual console (recommended first run)

A localhost web console over the same engine as the wizard — the same
philosophy Azure uses for its managed solutions (a declarative UI wizard in
front of the deployment template), built AWS-native:

```bash
gip console        # opens http://127.0.0.1:8321 in your browser
```

Four steps: **Basics** (region, identity provider, model) → **Modules**
(optional stacks as toggle cards; dependencies grey out with the reason shown)
→ **Review + create** (a rendered `answers.yaml` preview and validation) →
**Deploy progress** (live per-stack CloudFormation status and next steps).

![Console — Basics step](../images/console-basics.png)

The console never invents its own logic: every screen maps 1:1 onto the same
`answers.yaml` that `gip init --from-file` consumes, and the deploy button
drives the same code as `gip deploy`. Export the answers file at the Review
step to graduate to [Path 2 (GitOps)](#path-2-gitops-answers-file) with zero
rework. Localhost-only with a per-session API token. See
[CONSOLE.md](CONSOLE.md) for details and troubleshooting.

## Path 1 — Interactive wizard (credential-process default)

The standard flow documented in [QUICK_START.md](../../QUICK_START.md):

```bash
gip init       # ~30-question wizard: IdP, regions, models, quotas, monitoring
gip deploy     # CloudFormation stacks, dependency-ordered
gip package --go   # client binaries + config for macOS/Windows/Linux
gip test     # verify end-to-end with a real Bedrock call
gip distribute     # presigned URLs or authenticated landing page
```

Wizard progress is checkpointed and resumable; `gip doctor` diagnoses a
broken deployment. Reconfiguration = re-run `init` (existing answers are
preserved) and redeploy the affected stack.

## Path 2 — GitOps / answers file

Everything Path 1 does, driven from a version-controlled file with no TTY —
suited to orgs where infrastructure changes must flow through a pipeline:

```bash
gip init --from-file answers.yaml   # validates everything, zero prompts
gip deploy && gip package --go
```

The answers file mirrors the wizard's questions 1:1; bootstrap it from an
existing deployment with `gip init --export-answers answers.yaml`. Missing
or invalid fields fail with a complete list (CI-friendly, exit code 1). See
[CLI_REFERENCE.md — Non-interactive / GitOps mode](CLI_REFERENCE.md#non-interactive-gitops-mode).
Data-residency warnings print in this mode too.

## Path 3 — By hand (CloudFormation console)

For change-managed organizations that review templates, raise change
tickets, and deploy through the AWS console — no Python or CLI tooling on
the admin side. Every stack in `deployment/infrastructure/` is a plain
CloudFormation template deployable standalone. All standalone templates carry
`AWS::CloudFormation::Interface` metadata (grouped, plain-English parameter
labels), and [LAUNCH_STACKS.md](LAUNCH_STACKS.md) provides one-click
quick-create links once you publish the templates to an S3 bucket with
`scripts/publish-templates.sh`.

**Minimum viable deployment (auth only):**

1. Review [RESOURCE_INVENTORY.md](RESOURCE_INVENTORY.md) — per-stack resource
   lists and IAM actions, written for SCP pre-clearance and change review.
2. In the CloudFormation console, deploy
   `deployment/infrastructure/bedrock-auth-<your-idp>.yaml` (okta, azure,
   auth0, google, generic, or cognito-pool) with your IdP domain and client
   ID. `RestrictToAnthropicModels=true` is the default server-side model
   governance setting; set it to `false` only for approved non-Anthropic or
   opaque application-inference-profile deployments.
3. Copy the stack's **`ConfigurationJson` output** — this is the complete,
   ready-to-use client `config.json`. No packaging step required.
4. Build the **client binaries** (`credential-process` for each OS) once on an
   admin machine with Go 1.24+, or use `gip package`: in `source/go`, run `make`
   on macOS, or `make linux-x64 linux-arm64 windows` elsewhere (macOS binaries
   need a macOS host). Rename the output in `bin/` to `credential-process` when
   you install it. This sample does not publish pre-built binaries.
5. On each developer machine (by hand, script, or MDM):
   - place the binary and `config.json` in `~/gip/`,
   - add to `~/.aws/config`:
     ```ini
     [profile gip]
     credential_process = /path/to/credential-process --profile default
     region = <your-region>
     ```
   - set Claude Code to use Bedrock (`~/.claude/settings.json`):
     ```json
     {
       "env": {
         "CLAUDE_CODE_USE_BEDROCK": "1",
         "AWS_PROFILE": "gip",
         "ANTHROPIC_MODEL": "<cris-inference-profile-id>"
       }
     }
     ```
   Other harnesses (OpenCode, Aider, Pi) point at the same AWS profile — see
   [HARNESSES.md](HARNESSES.md).

**Optional stacks, in order** (each is an independent console deploy;
dependencies noted in [RESOURCE_INVENTORY.md](RESOURCE_INVENTORY.md)):
`networking → s3bucket → monitoring → dashboard → analytics → quota →
[codebuild] → [distribution] → [websearch] → [metering]`.

**Honest limits of the by-hand path:** the OTEL telemetry client settings
(collector endpoint, auth headers) and installer scripts are generated by
`gip package`; hand-writing them is possible but fiddly. A pragmatic hybrid
many traditional shops use: deploy all infrastructure by console (full
change control), then run `gip package` once on any workstation purely as a
file generator — it makes no AWS calls beyond reading stack outputs.

## Path 4 — Claude apps gateway (recommended for Claude apps)

Developers hold no AWS credentials. Use the exact AWS Samples gateway CDK
pinned in `vendor/aws-samples/anthropic-on-aws/UPSTREAM.json`; run
`scripts/fetch-claude-apps-gateway.sh` to materialize it under
`vendor/aws-samples/anthropic-on-aws/claude-apps-gateway`.
That upstream source owns the Fargate, ALB, PostgreSQL, image-build, policy,
telemetry, and Desktop `/user/bootstrap` contracts. Use its companion
`claude-apps-gateway-bootstrap` CDK only when a separate PKCE
configuration/MCP overlay is required. `gip deploy gateway/bootstrap` is
retired rather than adapting incompatible CloudFormation parameters. Trade-offs
versus credential-process paths are tabled in [APPS_GATEWAY.md](APPS_GATEWAY.md).

## Path 5 — Agent-assisted deployment

If your team already runs Claude Code (or OpenCode, Codex CLI, Kiro, etc.),
the repository is structured for agentic operation: `CLAUDE.md` files
describe every component, and [AGENT_DEPLOY.md](AGENT_DEPLOY.md) is a
step-by-step playbook an agent can execute with human checkpoints at the
two decisions that matter (IdP values, region/model selection) and
verification gates after every phase (`gip doctor`, `gip test`).
Hand the agent that file and your IdP details; expect ~1 hour supervised.

---

## Which path for which customer profile

| Customer profile | Recommended | Why |
|---|---|---|
| Claude Code / Claude Desktop only, any size | 4 (+1 for CI/IDC users) | First-party control plane; server-side model/spend policy; no per-dev credentials or packages |
| Startup / SMB, <50 devs, no dedicated platform team | 4 if Claude-only; else 5 (agent) or 1 | Gateway is one stack; wizard defaults are sane; consider Cognito user pool to skip external IdP setup |
| Mid-market with an IdP and an infra team | 1, then 2 for day-2 changes | Wizard first, export answers file, manage in git thereafter |
| Traditional enterprise, console-driven change management | 3 (+hybrid packaging) | Template review + change tickets per stack; no admin-side tooling |
| Regulated / air-gapped | 2 or 3 + `gip package --prepare-offline` | Reviewable artifacts, offline builds, RESOURCE_INVENTORY for SCP clearance |
| Multi-harness shop (OpenCode, Aider, Pi, Codex) | 1/2/3 + [HARNESSES.md](HARNESSES.md) | The credential-process plug governs every SDK-chain harness identically |

## Scaling to multiple accounts

Every path above targets one AWS account. Organizations that want one inference
account per Geo/Country/LoB under a central payer (AWS Organizations,
hub-and-spoke) should read [MULTI_ACCOUNT.md](MULTI_ACCOUNT.md) — it covers the
two supported topologies, what crosses account boundaries today, the SCP
library, and StackSets guidance (the auth stack is the StackSet-friendly piece;
`gip deploy` itself remains single-account).
