# Governed Inference Platform - CLI Reference

This document provides a complete reference for all `gip` (Governed Inference Platform) commands.

## Table of Contents

- [Governed Inference Platform - CLI Reference](#governed-inference-platform-cli-reference)
  - [Table of Contents](#table-of-contents)
  - [Overview](#overview)
  - [Installation](#installation)
  - [Command Reference](#command-reference)
    - [`init` - Configure Deployment](#init-configure-deployment)
      - [Non-interactive / GitOps mode](#non-interactive-gitops-mode)
    - [`console` - Visual Deployment Console](#console-visual-deployment-console)
    - [`deploy` - Deploy Infrastructure](#deploy-deploy-infrastructure)
    - [`test` - Test Package](#test-test-package)
    - [`package` - Create Distribution](#package-create-distribution)
    - [`builds` - List and Manage CodeBuild Builds](#builds-list-and-manage-codebuild-builds)
    - [`distribute` - Share Packages via Distribution](#distribute-share-packages-via-distribution)
    - [`status` - Check Deployment Status](#status-check-deployment-status)
    - [`cleanup` - Remove Installed Components](#cleanup-remove-installed-components)
  - [Quota Management](#quota-management)
    - [`quota set-user` - Set User Quota](#quota-set-user-set-user-quota)
    - [`quota set-group` - Set Group Quota](#quota-set-group-set-group-quota)
    - [`quota set-default` - Set Default Quota](#quota-set-default-set-default-quota)
    - [`quota list` - List Policies](#quota-list-list-policies)
    - [`quota delete` - Delete Policy](#quota-delete-delete-policy)
    - [`quota show` - Show Effective Quota](#quota-show-show-effective-quota)
    - [`quota usage` - Show Usage](#quota-usage-show-usage)
    - [`quota unblock` - Unblock User](#quota-unblock-unblock-user)
    - [`quota export` - Export Policies](#quota-export-export-policies)
    - [`quota import` - Import Policies](#quota-import-import-policies)
  - [Claude Cowork 3P](#claude-cowork-3p)
    - [`cowork generate` - Generate MDM Configuration](#cowork-generate-generate-mdm-configuration)
  - [Profile Management](#profile-management)
    - [`context list` - List All Profiles](#context-list-list-all-profiles)
    - [`context current` - Show Active Profile](#context-current-show-active-profile)
    - [`context use` - Switch Active Profile](#context-use-switch-active-profile)
    - [`context show` - Display Profile Details](#context-show-display-profile-details)
    - [`config validate` - Validate Profile Configuration](#config-validate-validate-profile-configuration)
    - [`config export` - Export Profile Configuration](#config-export-export-profile-configuration)
    - [`config import` - Import Profile Configuration](#config-import-import-profile-configuration)
    - [`destroy` - Remove Infrastructure](#destroy-remove-infrastructure)
    - [`doctor` - Validate Installation Health](#doctor-validate-installation-health)
    - [`models check` - Detect Model Catalog Drift](#models-check-detect-model-catalog-drift)
  - [Keeping the model catalog current](#keeping-the-model-catalog-current)

## Overview

The Governed Inference Platform CLI (`gip`) provides commands for IT administrators to:

- Configure OIDC authentication
- Deploy AWS infrastructure
- Create distribution packages
- Manage deployments

## Installation

```bash
# Clone the repository
git clone [<repository-url>](https://github.com/aws-samples/sample-guidance-for-governed-inference-platform-on-amazon-bedrock.git)
cd sample-guidance-for-governed-inference-platform-on-amazon-bedrock/source

# Install dependencies
poetry install

# Run commands with poetry
poetry run gip <command>
```

## Command Reference

### `init` - Configure Deployment

Creates or updates the configuration for your Claude Code deployment.

```bash
poetry run gip init [options]
```

**Options:**

- `--profile <name>` - Configuration profile name (optional, will prompt if not specified)
- `--managed` - Deploy settings to OS-level managed-settings.json (highest precedence, non-overridable by users)
- `--from-file <answers.(yaml|json)>` - Create a profile non-interactively from an answers file (see [Non-interactive / GitOps mode](#non-interactive-gitops-mode))
- `--export-answers <path>` - Export an existing profile back to an answers file (YAML or JSON, chosen by extension)
- `--profile-name <name>` - Profile name to create with `--from-file` or export with `--export-answers` (default: `default`)
- `--force` - Overwrite an existing profile when using `--from-file`

**What it does:**

- Checks prerequisites (AWS CLI, credentials, Python version)
- Prompts for OIDC provider configuration
- Prompts for authentication method selection:
  - Direct IAM: Uses IAM OIDC Provider for federation
  - Cognito: Uses Cognito Identity Pool for federation
- Configures AWS settings (region, stack names)
- Prompts for Claude model selection (Opus, Sonnet, Haiku)
- Configures cross-region inference profiles (US, Europe, APAC)
- Prompts for source region selection for model inference
- Sets up monitoring options
- Prompts for monitoring mode (central collector on ECS Fargate, or sidecar collector running locally on each developer's machine)
- Configures quota monitoring:
  - Monthly token limit per user
  - Daily token limit with burst buffer (auto-calculated from monthly)
  - Enforcement modes (alert vs block) for daily and monthly limits
  - Quota re-check interval (how often to verify quota with cached credentials)
- Prompts for Windows build support via AWS CodeBuild (optional)
- Saves configuration to `~/.gip/config.json`

**Note:** This command only creates configuration. Use `deploy` to create AWS resources.

#### Non-interactive / GitOps mode

`gip init --from-file` creates or updates a profile from a version-controlled answers file without asking a single question — no TTY is needed, so it works in CI pipelines. `--export-answers` does the reverse: it converts an existing profile back into a valid answers file, so wizard-created deployments can be moved under GitOps management.

```bash
# Create/update a profile from an answers file (no prompts, safe for CI)
poetry run gip init --from-file answers.yaml --profile-name prod

# Overwrite an existing profile
poetry run gip init --from-file answers.yaml --profile-name prod --force

# Export an existing profile back into an answers file (round-trip)
poetry run gip init --export-answers answers.yaml --profile-name prod
```

**Answers file schema:** the file mirrors the wizard's internal configuration structure — the same keys the interactive wizard collects. Every field you omit takes the same default as pressing Enter through the wizard for a minimal OIDC deployment. Only two fields have no possible default and must always be provided for OIDC: `okta.domain` and `okta.client_id`.

Top-level keys: `auth_type`, `sso_enabled`, `okta`, `provider_type`, `cognito_user_pool_id`, `oidc_issuer_url`, `oidc_authorization_endpoint`, `oidc_token_endpoint`, `oidc_jwks_uri`, `oidc_thumbprint`, `azure_auth_mode`, `client_certificate_path`, `client_certificate_key_path`, `credential_storage`, `redirect_port`, `federation_type`, `max_session_duration`, `idc_start_url`, `idc_account_id`, `idc_permission_set_name`, `sso_region`, `aws`, `monitoring`, `analytics`, `quota`, `metering`, `codebuild`, `guardrails`, `web_search`, `cowork_3p`, `cowork`, `settings_target`, `lock_default_model`, `distribution`, `tags`, `extra_files`.

Complete example — minimal Okta deployment with cost-based quota:

```yaml
# answers.yaml — minimal Okta + cost-mode quota deployment
okta:
  domain: company.okta.com        # REQUIRED — no default possible
  client_id: 0oa0000000000000000  # REQUIRED — no default possible (your Okta app client ID, starts with "0oa")

aws:
  region: us-east-1               # infrastructure region (Cognito/IAM/monitoring)
  identity_pool_name: gip-auth   # stack base name (max 20 chars)
  # selected_model / cross_region_profile / allowed_bedrock_regions /
  # selected_source_region default to Claude Sonnet 4.5 on the US
  # cross-region profile — set them here to pin a different model.

monitoring:
  enabled: true
  mode: sidecar                   # "sidecar" (local collector) or "central" (ECS Fargate)

quota:
  enabled: true
  limit_type: cost                # "cost" (USD budgets) or "token" (raw counts)
  monthly_cost_limit: 50.0        # USD per user per month
  daily_cost_limit: 0             # 0 = no daily cap
  monthly_enforcement_mode: block # "alert" or "block"
  daily_enforcement_mode: alert

metering:
  enabled: true                  # requires quota.enabled: true
  mode: shadow                   # "shadow" or "max"

tags:
  team: platform
```

**Behavior and guarantees:**

- **No prompts, ever.** With `--from-file`, no interactive question can fire. The command either succeeds or exits with code 1.
- **All problems at once.** Missing or invalid fields fail with the complete list of problems, not just the first — fix everything in one pass.
- **Typo protection.** Unknown keys anywhere in the file are a hard error listing the offending keys (e.g. `monitring` instead of `monitoring`).
- **Collision safety.** An existing profile is only overwritten with `--force`.
- **No secrets in the file.** Azure confidential-client secrets (`client_secret`) are rejected — they live in the OS keyring only (`credential-process --set-client-secret`). Landing-page distribution secrets must be pre-created in AWS Secrets Manager and referenced via `distribution.idp_client_secret_arn`.
- **Auth coverage.** All three auth modes work: OIDC (default), `auth_type: idc` (requires `idc_start_url` + `idc_account_id`), and `auth_type: none`.

**Export round-trip.** `--export-answers` writes an answers file that reproduces the profile exactly when re-imported:

```bash
# Move a wizard-created profile under GitOps management
poetry run gip init --export-answers answers.yaml --profile-name prod
git add answers.yaml && git commit -m "Manage prod gip profile via GitOps"

# Later, in CI (or on another admin machine):
poetry run gip init --from-file answers.yaml --profile-name prod --force
poetry run gip deploy
```

Fields the wizard handles that can never be expressed in the answers file: raw client secrets (keyring / Secrets Manager only, see above). Fields with generated values: `cowork_3p.service_token` is auto-generated (UUID) for central monitoring mode when absent, exactly like the wizard.

### `console` - Visual Deployment Console

Serves a localhost single-page wizard that authors the same answers file `init --from-file` consumes, creates the profile through the same code path, and deploys through the same engine as `deploy`.

```bash
poetry run gip console [options]
```

**Options:**

- `--port <port>` - Port to serve on, 127.0.0.1 only (default: 8321)
- `--no-browser` - Print the URL without auto-opening a browser

**What it does:**

- Serves a four-step wizard (Basics -> Modules -> Review + create -> Progress) on `http://127.0.0.1:<port>/` and opens your browser
- Validates input with the exact `init --from-file` validators and previews the generated `answers.yaml` (exportable for GitOps)
- Creates/updates profiles in `~/.gip/profiles/` identically to `gip init --from-file`
- Runs `gip deploy` (all enabled stacks or a per-stack selection, with dry-run) in the background and streams per-stack progress

**Security:** binds 127.0.0.1 only; every API call requires a per-session token embedded in the served page (blocks drive-by localhost requests from other websites). See [Console guide](CONSOLE.md).

**Examples:**

```bash
# Launch on the default port and open the browser
poetry run gip console

# Headless / over SSH (forward the port, open the printed URL locally)
poetry run gip console --port 9000 --no-browser
```

### `deploy` - Deploy Infrastructure

Deploys CloudFormation stacks for authentication and monitoring.

```bash
poetry run gip deploy [stack] [options]
```

**Arguments:**

- `stack` - Specific stack to deploy: auth, guardrails, networking, monitoring, dashboard, analytics, quota, metering, model-lifecycle, websearch, memory, gateway, bootstrap, skills, distribution, codebuild, s3bucket, or cowork-dashboard (optional)

**Options:**

- `--profile <name>` - Configuration profile to use (default: "default")
- `--dry-run` - Show what would be deployed without executing
- `--show-commands` - Display AWS CLI commands instead of executing

**What it does:**

- Deploys authentication infrastructure (IAM OIDC Provider or Cognito Identity Pool)
- Creates IAM roles and policies for Bedrock access
- Deploys monitoring infrastructure (if enabled)
- Shows stack outputs including authentication resource identifiers

**Stacks deployed:**

1. **auth** - Authentication infrastructure and IAM roles (always required)
2. **networking** - VPC and networking resources for monitoring (central mode only)
3. **monitoring** - OpenTelemetry collector on ECS Fargate (central mode only)
4. **dashboard** - CloudWatch dashboard for usage metrics (optional)
5. **analytics** - Kinesis Firehose and Athena SQL query pipeline (central mode only, optional)
6. **quota** - Per-user token quota monitoring and alerts (optional, requires dashboard)
7. **guardrails** - Account-level Bedrock Guardrails enforcement in each configured Bedrock region (optional)
8. **codebuild** - AWS CodeBuild for Windows binary builds (optional, only if enabled during init)

> **Note**: In sidecar monitoring mode, the auth and dashboard stacks are deployed. The networking, monitoring, and analytics stacks are skipped because the OpenTelemetry collector runs locally on each developer's machine. Both modes include the same PromQL CloudWatch dashboard with full metric analytics.

**Examples:**

```bash
# Deploy all configured stacks
poetry run gip deploy

# Deploy only authentication
poetry run gip deploy auth

# Deploy quota monitoring (requires dashboard stack first)
poetry run gip deploy quota

# Deploy Bedrock Guardrails enforcement in configured Bedrock regions
poetry run gip deploy guardrails

# Show commands without executing
poetry run gip deploy --show-commands

# Dry run to see what would be deployed
poetry run gip deploy --dry-run
```

> **Note**: Quota monitoring requires the dashboard stack to be deployed first. See [Quota Monitoring Guide](QUOTA_MONITORING.md) for detailed information.

#### When to Use `gip deploy` vs `gip deploy quota`

| Command | Use Case |
|---------|----------|
| `gip deploy` | Initial setup - deploys all enabled stacks including quota (when enabled) |
| `gip deploy quota` | Update quota settings, late enablement, or troubleshooting |

**When `gip deploy` deploys quota**: If `quota_monitoring_enabled=True` in your profile (set during `gip init`), running `gip deploy` will automatically deploy the quota stack as part of the full deployment.

**When to use `gip deploy quota`**:
- You want to update quota configuration without redeploying other stacks
- You initially deployed without quota and now want to add it
- You need to troubleshoot or redeploy just the quota stack
- Your organization requires phased deployments with explicit control

### `test` - Test Package

Tests the packaged distribution as an end user would experience it.

```bash
poetry run gip test [options]
```

**Options:**

- `--profile, -p <name>` - Profile name to test (defaults to active profile)
- `--full` - Test all allowed regions (default: tests 3 representative regions)
- `--quota-only` - Run only quota monitoring tests (API, policies, usage capture)
- `--quota-api <endpoint>` - Test quota API with optional custom endpoint override

**What it does:**

- Finds the latest package for the profile in `dist/{profile}/{timestamp}/`
- Verifies package contents (binary, config, OTEL helper)
- Tests credential process binary execution
- Tests authentication and IAM role assumption
- Tests Bedrock API access in configured regions
- Tests inference profile availability
- Tests quota monitoring API (if enabled)

**Quota Testing (`--quota-only`):**

When using `--quota-only`, runs comprehensive quota monitoring tests:

1. **Quota Config** - Validates all quota configuration is present
2. **Quota API** - Tests the `/check` endpoint with JWT authentication
3. **Create Policy** - Creates a test user policy in DynamoDB
4. **List Policies** - Verifies the policy appears in the list
5. **Resolve Quota** - Tests policy resolution for users
6. **Delete Policy** - Cleans up the test policy

**Examples:**

```bash
# Run standard tests
poetry run gip test

# Run only quota monitoring tests (fastest for quota validation)
poetry run gip test --quota-only

# Test quota API against a staging endpoint
poetry run gip test --quota-only --quota-api https://staging-api.example.com/prod

# Run all tests with custom quota endpoint
poetry run gip test --quota-api https://my-api.execute-api.us-east-1.amazonaws.com/prod
```

**Note:** API tests run by default and make actual calls to Bedrock (minimal cost ~$0.001).

### `package` - Create Distribution

Creates a distribution package for end users.

```bash
poetry run gip package [options]
```

**Options:**

- `--target-platform <platform>` - Target platform for binary; by default, macOS hosts build all five targets and other hosts build Linux x64/ARM64 plus Windows
  - `macos-arm64` - Apple Silicon Macs (M1/M2/M3/M4)
  - `macos-intel` - Intel Macs
  - `linux-x64` - Linux x86-64
  - `linux-arm64` - Linux ARM64 (Graviton, etc.)
  - `windows` - Windows x64
  - `all` - All 5 platforms
- `--go` - Use the default Go build path; retained for backward compatibility
- `--legacy` - Use the deprecated PyInstaller/Nuitka build path
- `--build-verbose` - Enable verbose build-process logging
- `--build-local` - Deprecated no-op retained for compatibility; Go builds are always local
- `--no-cache` - Deprecated no-op retained for compatibility; no pre-built binary cache is used
- `--profile <name>` - Configuration profile to use [default: active profile]
- `--regenerate-installers` - Regenerate config and install scripts using existing binaries from latest dist
- `--harnesses <names>` - Generate configs for selected coding harnesses
- `--skip-validation` - Skip configuration validation checks
- `--prepare-offline` - Prepare an offline binary and Go-module-cache bundle
- `--status <id|latest>` - Deprecated status lookup; use `gip builds`

Package upload is a separate command: `gip distribute --expires-hours <hours>`.

**What it does:**

1. Cross-compiles native Go binaries for all selected platforms
2. Creates `config.json` with federation config read from the admin profile
3. Creates `gip-settings/settings.json` with Bedrock model and OTel endpoint (or `managed-settings.json` if `--managed` was used during init)
4. Creates installer scripts (`install.sh`, `install.bat`, `gip-install.ps1`)
5. Outputs to `dist/{profile}/{timestamp}/`

**Build Modes:**

| Mode | Flag | Requirements | Best for |
|---|---|---|---|
| **Go cross-compile** (default) | none (`--go` is accepted for compatibility) | Go 1.24+ installed | Most administrators |
| **Legacy** (deprecated) | `--legacy` | PyInstaller, Docker, CodeBuild | Temporary compatibility with Python binaries |

**Platform Support (Go Cross-Compilation):**

The Go build path requires Go 1.24+. Linux and Windows targets cross-compile from macOS, Linux, or Windows without Docker or CodeBuild. The macOS credential process uses CGO for Keychain access, so macOS ARM64 and Intel targets require a macOS build host with Apple build tools. Consequently, `--target-platform all` must run on macOS; Linux and Windows administrators should request only non-macOS targets.

- **macOS ARM64**: Native Apple Silicon binary (~9 MB)
- **macOS Intel**: Native x86-64 binary (~10 MB)
- **Linux x64**: Statically linked, works on any distro (~10 MB)
- **Linux ARM64**: Statically linked for Graviton/ARM (~9 MB)
- **Windows x64**: Native PE with embedded version info (~14 MB, unstripped for Defender compatibility)

**Offline Packaging:**

`gip package` normally needs internet on the admin machine for three things: Go module downloads for `source/go`, the OCB (OpenTelemetry Collector Builder) binary from GitHub, and Go module downloads for the OCB-generated collector module (sidecar mode only). A `vendor/` directory cannot cover the collector build — OCB generates a fresh Go module in a temp directory on every run.

For air-gapped environments, use `--prepare-offline` to pre-seed everything:

```bash
# On an internet-connected machine (same OS/arch and Go version as the offline box):
poetry run gip package --prepare-offline
# Transfer gip-offline-go-bundle.tar.gz to the offline machine, extract, then:
tar xzf gip-offline-go-bundle.tar.gz
./scripts/prepare-offline-go-bundle.sh install
source gip-offline-go-bundle/offline-env.sh
poetry run gip package
```

The bundle contains the pinned OCB binary (installed to `~/.cache/ocb/`, where `package.py` looks before downloading) and a pre-populated Go module cache covering both `source/go` and the collector. Because every `go`/`ocb` subprocess inherits the environment, `GOPROXY=off` plus the seeded `GOMODCACHE` satisfies all module resolution — including the `go mod tidy` OCB runs internally. The `prepare` step rehearses a fully offline collector build before archiving, so a bundle that ships is a bundle that works.

**Legacy mode platform details (PyInstaller / Nuitka / Docker):**

PyInstaller is a runtime bundler, not a cross-OS compiler. It emits binaries in the host OS's native format (Mach-O on macOS, ELF on Linux). That constrains which targets each build host can produce:

| Target binary | Build host required | Tooling |
|---|---|---|
| `macos-arm64`, `macos-intel` | **macOS** | PyInstaller (native) |
| `linux-x64`, `linux-arm64` | Linux, **or** macOS with Docker Desktop | PyInstaller (Docker container when building from macOS) |
| `windows` | any host | AWS CodeBuild (remote) |

> **Linux admins cannot build macOS binaries.** There is no supported path for producing Mach-O binaries on Linux — Apple's platform design makes this infeasible. If `gip package` detects a macOS target on a non-macOS host, the build refuses with a clear error rather than silently producing an ELF binary labeled as macOS (which would fail on end-user Macs with `exec format error`).
>
> To produce macOS binaries, use a macOS workstation, a CI macOS runner (GitHub Actions `macos-latest`, AWS CodeBuild macOS project, or a self-hosted Mac runner), or an EC2 Mac instance, and collect the artifacts from there.

- **macOS**: Uses PyInstaller with architecture-specific builds
  - ARM64: Native build on Apple Silicon Macs only — cannot run on Intel Macs
  - Intel: Runs natively on Intel Macs and on Apple Silicon via Rosetta — covers all Mac users with one binary
  - Cross-arch: **Optional** — build the other architecture from your current Mac; requires a universal2 Python (see below)
- **Linux**: Uses PyInstaller in Docker containers (cross-compiled from macOS host)
  - x64: Uses linux/amd64 Docker platform
  - ARM64: Uses linux/arm64 Docker platform
  - Docker Desktop handles architecture emulation automatically
  - **Requires Docker Desktop to be installed and running** — see Graceful Fallback Behavior below
  - On a Linux host, PyInstaller runs natively — Docker is not required
- **Windows**: Uses Nuitka via AWS CodeBuild (if enabled during init)
  - Automated builds take 12-15 minutes
  - Requires CodeBuild to be enabled during `init`
  - Will be skipped if CodeBuild is not enabled

#### Cross-arch macOS Build Setup (legacy mode only, optional) {#cross-arch-macos-build-setup-optional}

By default, legacy-mode `gip package` builds a binary for your Mac's own architecture. The Intel (`macos-intel`) binary covers all Mac users — it runs natively on Intel Macs and via Rosetta on Apple Silicon — so an Apple Silicon admin who needs to support Intel Mac users should build the Intel binary using this setup.

To build for the other architecture (e.g. Intel binary on Apple Silicon, or ARM64 binary on Intel), install a universal2 Python:

1. Download the **macOS 64-bit universal2 installer** for Python 3.12 from [python.org/downloads/macos](https://www.python.org/downloads/macos/)
2. Run the installer — it places Python at `/Library/Frameworks/Python.framework/`
3. Re-run `gip package` — it detects the universal2 Python automatically

`gip` creates an isolated per-arch build environment at `~/.gip/build-venvs/` on first cross-arch build (~30s). Subsequent runs reuse it.

(With `--go`, cross-arch builds are unnecessary — Go cross-compiles all platforms natively.)

**Behavior when universal2 Python is not installed:**

- For `--target-platform=all`: Skips the cross-arch target with a note, builds all other platforms normally
- For an explicit cross-arch target (e.g. `--target-platform=macos-intel` on Apple Silicon): Fails with a clear error pointing to the python.org installer
- The package process continues successfully without cross-arch binaries
- Note: Intel (`macos-intel`) binaries run natively on Intel Macs and via Rosetta on Apple Silicon — they cover all Mac users. ARM64 binaries only run on Apple Silicon and cannot run on Intel Macs.

**Graceful Fallback Behavior (legacy mode):**

The package command is designed to handle missing optional components gracefully:

- **Cross-arch macOS builds**: Skipped if universal2 Python is not installed (see Cross-arch macOS Build Setup above)
- **Windows builds**: Skipped if CodeBuild was not enabled during `init`
- **Linux builds (from macOS)**: Skipped with a warning in two cases:
  - Docker is not installed (`docker` binary not found in `$PATH`) — install Docker Desktop from https://docs.docker.com/get-docker/
  - Docker is installed but the daemon is not running — open Docker Desktop and wait for it to start, then retry
  - macOS and Windows builds are **unaffected** by Docker availability
- **macOS builds (from non-macOS host)**: Refused with a clear error explaining that PyInstaller cannot cross-compile and listing alternative paths (macOS workstation, CI runner, EC2 Mac). Other targets in the same `gip package` invocation continue to build normally.
- **At least one platform must build successfully** for the package command to succeed

This ensures that packaging always works, even if some optional platforms are not available.

**Output files:**

- `credential-process-<platform>` - Authentication executable
  - `credential-process-macos-arm64` - macOS Apple Silicon
  - `credential-process-macos-intel` - macOS Intel
  - `credential-process-linux-x64` - Linux x64
  - `credential-process-linux-arm64` - Linux ARM64
  - `credential-process-windows.exe` - Windows x64
- `otel-helper-<platform>` - OTEL helper (if monitoring enabled)
- `config.json` - Configuration
- `install.sh` - Unix installer script (auto-detects architecture)
- `install.bat` - Windows installer launcher
- `gip-install.ps1` - Windows PowerShell installer logic (called by install.bat)
- `README.md` - Installation instructions
- Includes Claude Code telemetry settings (if monitoring enabled)
- Configures environment variables for model selection (ANTHROPIC_MODEL, ANTHROPIC_SMALL_FAST_MODEL)

**Credential process binary flags (for end users):**

The distributed `credential-process` binary accepts the following flags directly:

| Flag | Description |
|---|---|
| `--profile, -p <name>` | Profile to use (default: `gip`, or `$GIP_PROFILE`) |
| `--clear-cache` | Clear cached credentials and force re-authentication |
| `--check-expiration` | Exit 0 if credentials valid, 1 if expired |
| `--refresh-if-needed` | Refresh credentials if expired (session storage mode only) |
| `--get-monitoring-token` | Return cached OIDC monitoring token |
| `--set-client-secret` | Store Azure AD client secret in OS secure storage. Uses an interactive prompt by default; set `GIP_CLIENT_SECRET` env var for non-interactive use. Press Enter at the prompt (or set the env var to an empty string) to clear the stored secret. |

**`--set-client-secret` usage examples:**

```bash
# Interactive (prompts for secret):
~/gip/credential-process --set-client-secret --profile gip

# Non-interactive (MDM/scripted deployment) — avoids secret appearing in shell history:
GIP_CLIENT_SECRET=<your-client-secret> ~/gip/credential-process --set-client-secret --profile gip

# Clear a stored secret:
~/gip/credential-process --set-client-secret --profile gip
# (press Enter without typing a value)
```

**Certificate path environment variables (confidential client — certificate mode):**

When certificate paths recorded in `config.json` are absolute, they may not resolve on end-user machines with a different install layout. Set these env vars to override the paths stored in `config.json` at runtime:

| Environment variable | Description |
|---|---|
| `AZURE_CLIENT_CERTIFICATE_PATH` | Path to the PEM certificate file. Overrides `client_certificate_path` in `config.json`. |
| `AZURE_CLIENT_CERTIFICATE_KEY_PATH` | Path to the PEM private key file. Overrides `client_certificate_key_path` in `config.json`. |

```bash
# Override certificate paths (e.g. via MDM launch agent environment):
AZURE_CLIENT_CERTIFICATE_PATH=~/certs/cert.pem \
AZURE_CLIENT_CERTIFICATE_KEY_PATH=~/certs/key.pem \
~/gip/credential-process --profile gip
```

**Output structure:**

```
dist/
├── credential-process-macos-arm64     # macOS ARM64 executable
├── credential-process-macos-intel     # macOS Intel executable
├── credential-process-linux-x64       # Linux x64 executable
├── credential-process-linux-arm64     # Linux ARM64 executable
├── credential-process-windows.exe     # Windows x64 executable
├── otel-helper-macos-arm64           # macOS ARM64 OTEL helper
├── otel-helper-macos-intel           # macOS Intel OTEL helper
├── otel-helper-linux-x64             # Linux x64 OTEL helper
├── otel-helper-linux-arm64           # Linux ARM64 OTEL helper
├── otel-helper-windows.exe           # Windows OTEL helper
├── config.json                       # Configuration
├── install.sh                        # Unix installer (auto-detects architecture)
├── install.bat                       # Windows installer launcher
├── gip-install.ps1                  # Windows PowerShell installer logic
├── README.md                         # User instructions
└── .claude/
    └── settings.json                 # Telemetry settings (optional)
```

### `builds` - List and Manage CodeBuild Builds

Shows recent Windows binary builds and their status.

```bash
poetry run gip builds [options]
```

**Options:**

- `--profile <name>` - Configuration profile to use (defaults to active profile)
- `--limit <n>` - Number of builds to show (default: "10")
- `--project <name>` - CodeBuild project name (default: auto-detect)
- `--status <id>` - Check status of a specific build by ID
- `--download` - Download completed Windows artifacts to dist folder

**What it does:**

- Lists recent CodeBuild builds for Windows binaries
- Shows build status, duration, and completion time
- Provides console links to view full build logs
- Monitors in-progress builds
- Uses active profile or specified profile for CodeBuild project detection

**Note:** This command requires CodeBuild to be enabled during the `init` process. If CodeBuild was not enabled, you'll need to re-run `init` and enable Windows build support.

**Examples:**

```bash
# List builds for active profile
poetry run gip builds

# List builds for specific profile
poetry run gip builds --profile production

# Check status of specific build
poetry run gip builds --status abc12345

# Check latest build status and download artifacts
poetry run gip builds --status latest --download

# List last 20 builds
poetry run gip builds --limit 20
```

**Example output:**

```
Recent Windows Builds

| Build ID | Status | Started | Duration |
|----------|--------|---------|----------|
| project:abc123 | SUCCEEDED | 2024-08-26 10:15 | 12m 34s |
| project:def456 | IN_PROGRESS | 2024-08-26 10:30 | - |
```

### `distribute` - Share Packages via Distribution

Upload and distribute built packages via presigned S3 URLs or authenticated landing page.

```bash
poetry run gip distribute [options]
```

**Options:**

- `--expires-hours <hours>` - URL expiration time in hours (1-168) [default: "48"]
- `--get-latest` - Retrieve the latest distribution URL (presigned-s3 only)
- `--profile <name>` - Configuration profile to use (uses active profile if not specified)
- `--package-path <path>` - Path to package directory [default: "dist"]
- `--build-profile <name>` - Select build by profile name
- `--timestamp <timestamp>` - Select build by timestamp (format: YYYY-MM-DD-HHMMSS)
- `--latest` - Auto-select latest build without wizard
- `--allowed-ips <ranges>` - Unsupported; the command fails before packaging or upload because S3 presigned URLs do not enforce source-IP restrictions
- `--show-qr` - Display QR code for URL (requires qrcode library)

**What it does:**

Behavior depends on your configured distribution type:

**Presigned S3 URLs (Simple):**
- Uploads packages to S3 bucket
- Generates secure presigned URLs (default 48 hours)
- Stores URLs in Parameter Store for team access
- Share URLs via email/Slack
- No authentication required for downloads

**Landing Page (Enterprise):**
- Uploads platform-specific packages (windows/linux/mac/all-platforms)
- Updates S3 metadata (profile, timestamp, release date)
- Provides landing page URL for authenticated access
- Users authenticate via IdP (Okta/Azure/Auth0/Cognito)
- Platform auto-detection and recommendations

**Distribution workflow:**

1. Build packages: `poetry run gip package`
2. Upload and distribute: `poetry run gip distribute`
3. **Presigned-s3**: Share generated URLs with developers
4. **Landing-page**: Direct users to your landing page URL

**Examples:**

```bash
# Distribute latest build (interactive build selection)
poetry run gip distribute

# Distribute latest build automatically (skip wizard)
poetry run gip distribute --latest

# Distribute specific build by timestamp
poetry run gip distribute --timestamp 2024-11-14-083022

# Distribute with custom expiration (presigned-s3 only)
poetry run gip distribute --expires-hours=72

# Get existing URL without re-uploading (presigned-s3 only)
poetry run gip distribute --get-latest

# Distribute with QR code for mobile sharing
poetry run gip distribute --show-qr
```

**Build Selection:**

If you have multiple builds in `dist/`, the command will:
1. Scan for organized profile/timestamp builds
2. Show interactive wizard to select which build to distribute
3. Display build date, size, and platforms included
4. Allow selection by profile name or timestamp

Use `--latest` to skip the wizard and auto-select the most recent build.

**Platform-Specific Uploads (Landing Page):**

For landing-page distribution, packages are organized by platform:
- `packages/windows/latest.zip` - Windows package
- `packages/linux/latest.zip` - Linux package
- `packages/mac/latest.zip` - macOS package
- `packages/all-platforms/latest.zip` - All platforms bundle

Landing page auto-detects user's OS and recommends appropriate package.

### `status` - Check Deployment Status

Shows the current deployment status and configuration.

```bash
poetry run gip status [options]
```

**Options:**

- `--profile <name>` - Profile to check (uses active profile if not specified)
- `--json` - Output in JSON format
- `--detailed` - Show detailed information

**What it does:**

- Shows current configuration including:
  - Configuration profile and AWS profile names
  - OIDC provider and client ID
  - Selected Claude model and cross-region profile
  - Source region for model inference
  - Analytics and monitoring status
- Checks CloudFormation stack status
- Displays Identity Pool information
- Shows monitoring configuration and endpoints
- In sidecar monitoring mode, shows local collector status (running/stopped) and the local OTLP endpoint

### `cleanup` - Remove Installed Components

Removes components installed by the test command or manual installation.

```bash
poetry run gip cleanup [options]
```

**Options:**

- `--force` - Skip confirmation prompts
- `--profile <name>` - AWS profile name to remove (default: "gip")

**What it does:**

- Removes `~/gip/` directory
- Removes AWS profile from `~/.aws/config`
- Removes Claude settings from `~/.claude/settings.json`
- Shows what will be removed before taking action

**Use this to:**

- Clean up after testing
- Remove failed installations
- Start fresh with a new configuration

## Claude Cowork 3P

### `cowork generate` - Generate MDM Configuration

Generate Claude Cowork 3P MDM configuration files for deploying Claude Desktop with Amazon Bedrock as the inference backend.

This command reads your existing deployment profile (region, model, monitoring stack) and generates ready-to-deploy MDM configuration files.

```bash
# Generate all formats (JSON, macOS .mobileconfig, Windows .reg)
poetry run gip cowork generate

# Generate specific format
poetry run gip cowork generate --format mobileconfig
poetry run gip cowork generate --format reg
poetry run gip cowork generate --format json

# Custom model aliases
poetry run gip cowork generate --models opus,sonnet,haiku

# Custom output directory
poetry run gip cowork generate -o ./my-mdm-configs/

# Specific profile
poetry run gip cowork generate --profile Production
```

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `--profile` | Configuration profile to use | Active profile |
| `--output`, `-o` | Output directory | `dist/cowork-3p/` |
| `--format`, `-f` | Output format: `all`, `json`, `mobileconfig`, `reg` | `all` |
| `--models`, `-m` | Comma-separated model aliases | Auto-detected from profile |

**Generated files:**

| File | Platform | Description |
|------|----------|-------------|
| `cowork-3p-config.json` | All | Raw MDM configuration JSON (for Claude Desktop Setup UI import) |
| `cowork-3p.mobileconfig` | macOS | MDM configuration profile (deploy via Jamf, Kandji, Mosyle) |
| `cowork-3p.reg` | Windows | Registry file (deploy via Group Policy, Intune, SCCM) |

**Automatic integration with `gip package`:**

CoWork 3P configs are also auto-generated during `gip package` when enabled via `gip init`. Both paths use the same shared configuration logic to ensure identical output.

See [CoWork 3P Guide](COWORK_3P.md) for detailed setup and deployment instructions.

## Quota Management

Commands for managing per-user and group token quotas. Requires quota monitoring to be enabled during `init`.

For detailed architecture and configuration, see [QUOTA_MONITORING.md](QUOTA_MONITORING.md).

### `quota set-user` - Set User Quota

Sets a quota policy for a specific user.

```bash
poetry run gip quota set-user <email> [options]
```

**Arguments:**
- `<email>` - User's email address

**Options:**
- `--monthly-limit, -m <tokens>` - Monthly token limit (supports K, M, B suffixes: 10M = 10,000,000)
- `--daily-limit, -d <tokens>` - Daily token limit (optional)
- `--enforcement, -e <mode>` - Enforcement mode: `alert` (monitor only) or `block` (deny access)
- `--disabled` - Create policy in disabled state
- `--profile, -p <name>` - Configuration profile

**Example:**
```bash
poetry run gip quota set-user alice@example.com -m 5M -e block
```

### `quota set-group` - Set Group Quota

Sets a quota policy for a group (applies to all users in the group).

```bash
poetry run gip quota set-group <group> [options]
```

**Arguments:**
- `<group>` - Group name (from OIDC groups claim)

**Options:**
- Same as `set-user`

**Example:**
```bash
poetry run gip quota set-group engineering -m 20M -d 1M -e alert
```

### `quota set-default` - Set Default Quota

Sets the default quota policy for all users without a specific user or group policy.

```bash
poetry run gip quota set-default [options]
```

**Options:**
- Same as `set-user`

**Example:**
```bash
poetry run gip quota set-default -m 225M -e alert
```

### `quota list` - List Policies

Lists all quota policies.

```bash
poetry run gip quota list [options]
```

**Options:**
- `--type <type>` - Filter by type: `user`, `group`, or `default`
- `--profile, -p <name>` - Configuration profile

### `quota delete` - Delete Policy

Deletes a quota policy.

```bash
poetry run gip quota delete <type> <identifier> [options]
```

**Arguments:**
- `<type>` - Policy type: `user`, `group`, or `default`
- `<identifier>` - Email (for user), group name, or "default"

**Options:**
- `--profile, -p <name>` - Configuration profile

**Example:**
```bash
poetry run gip quota delete user alice@example.com
```

### `quota show` - Show Effective Quota

Shows the effective quota policy for a user (resolves user > group > default precedence).

```bash
poetry run gip quota show <email> [options]
```

**Arguments:**
- `<email>` - User's email address

**Options:**
- `--profile, -p <name>` - Configuration profile

### `quota usage` - Show Usage

Shows current usage against quota limits for a user.

```bash
poetry run gip quota usage <email> [options]
```

**Arguments:**
- `<email>` - User's email address

**Options:**
- `--profile, -p <name>` - Configuration profile

### `quota unblock` - Unblock User

Temporarily unblocks a user who has been blocked due to quota exceeded.

```bash
poetry run gip quota unblock <email> [options]
```

**Arguments:**
- `<email>` - User's email address

**Options:**
- `--duration <time>` - Duration: `24h`, `7d`, `until-reset`, or custom (e.g., `48h`, `3d`)
- `--reason <text>` - Reason for unblock (for audit trail)
- `--profile, -p <name>` - Configuration profile

**Example:**
```bash
poetry run gip quota unblock alice@example.com --duration 24h --reason "Emergency project deadline"
```

### `quota export` - Export Policies

Exports quota policies to a JSON or CSV file for backup, migration, or auditing.

```bash
poetry run gip quota export <file> [options]
```

**Arguments:**
- `<file>` - Output file path (.json or .csv)

**Options:**
- `--type, -t <type>` - Filter by policy type: `user`, `group`, or `default`
- `--stdout` - Output to stdout instead of file
- `--profile, -p <name>` - Configuration profile

**Examples:**
```bash
# Export all policies to JSON
poetry run gip quota export policies.json

# Export to CSV for spreadsheet editing
poetry run gip quota export policies.csv

# Export only user policies
poetry run gip quota export users.json --type user

# Export to stdout (for piping)
poetry run gip quota export --stdout > backup.json
```

**JSON output format:**
```json
{
  "version": "1.0",
  "exported_at": "2025-11-29T10:30:00Z",
  "policies": [
    {
      "type": "user",
      "identifier": "alice@example.com",
      "monthly_token_limit": "300M",
      "daily_token_limit": "15M",
      "enforcement_mode": "alert",
      "enabled": true
    }
  ]
}
```

**CSV output format:**
```csv
type,identifier,monthly_token_limit,daily_token_limit,enforcement_mode,enabled
user,alice@example.com,300M,15M,alert,true
group,engineering,500M,25M,block,true
default,default,225M,8M,alert,true
```

### `quota import` - Import Policies

Imports quota policies from a JSON or CSV file. Supports bulk policy creation with conflict handling.

```bash
poetry run gip quota import <file> [options]
```

**Arguments:**
- `<file>` - Input file path (.json or .csv)

**Options:**
- `--skip-existing` - Skip policies that already exist
- `--update` - Update existing policies (upsert mode)
- `--dry-run` - Preview changes without applying
- `--type, -t <type>` - Import only specific type: `user`, `group`, or `default`
- `--auto-daily` - Auto-calculate daily limits for policies missing `daily_token_limit`
- `--burst <percent>` - Burst buffer percentage for auto-daily calculation (default: 10)
- `--profile, -p <name>` - Configuration profile

**Examples:**
```bash
# Import from JSON, skip existing policies
poetry run gip quota import policies.json --skip-existing

# Import from CSV, update existing policies
poetry run gip quota import policies.csv --update

# Preview import without making changes
poetry run gip quota import policies.json --dry-run

# Import users only
poetry run gip quota import all-policies.csv --type user --update

# Auto-calculate daily limits with 15% burst buffer
poetry run gip quota import users.csv --auto-daily --burst 15
```

**Output example:**
```
✓ Created: alice@example.com (user) - 300M
✓ Created: bob@example.com (user) - 200M
⚠ Skipped: engineering (group) - already exists
✓ Updated: ml-team (group) - 1B

Import Summary
  Created: 2
  Updated: 1
  Skipped: 1
  Errors:  0
```

**Required CSV columns:**
- `type` - Policy type: `user`, `group`, or `default`
- `identifier` - User email, group name, or `default`
- `monthly_token_limit` - Monthly limit (supports K/M/B suffix, e.g., `300M`)

**Optional CSV columns:**
- `daily_token_limit` - Daily limit (auto-calculated if `--auto-daily`)
- `enforcement_mode` - `alert` (default) or `block`
- `enabled` - `true` (default) or `false`

## Profile Management

The following commands manage multiple deployment profiles (v2.0+). Profiles let you manage configurations for different AWS accounts, regions, or organizations from a single machine.

### `context list` - List All Profiles

Shows all available profiles with an indicator for the active profile.

```bash
poetry run gip context list
```

**What it does:**

- Lists all profiles in `~/.gip/profiles/`
- Displays profile name, AWS region, and stack name
- Highlights the currently active profile
- Shows profile count

**Example output:**

```
Available Profiles:
  * production (us-east-1, stack: gip-prod)
    development (us-west-2, stack: gip-dev)
    eu-deployment (eu-west-1, stack: gip-eu)

Active profile: production
Total profiles: 3
```

### `context current` - Show Active Profile

Displays the currently active profile name.

```bash
poetry run gip context current
```

**What it does:**

- Shows the name of the active profile
- Exits with error if no active profile is set

**Example output:**

```
Current profile: production
```

### `context use` - Switch Active Profile

Changes the active profile to the specified one.

```bash
poetry run gip context use <profile-name>
```

**Arguments:**

- `profile-name` - Name of the profile to activate (required)

**What it does:**

- Sets the specified profile as active
- Validates that the profile exists
- Updates global configuration file

**Examples:**

```bash
# Switch to production profile
poetry run gip context use production

# Switch to development profile
poetry run gip context use development
```

### `context show` - Display Profile Details

Shows detailed configuration for a profile.

```bash
poetry run gip context show [profile-name]
```

**Arguments:**

- `profile-name` - Profile to display (optional, defaults to active profile)

**Options:**

- `--json` - Output in JSON format

**What it does:**

- Displays full profile configuration including:
  - AWS region and account
  - OIDC provider settings
  - Stack names
  - Model selection
  - Monitoring configuration
- Masks sensitive values (client secrets)

**Examples:**

```bash
# Show active profile details
poetry run gip context show

# Show specific profile
poetry run gip context show production

# Output as JSON
poetry run gip context show --json
```

### `config validate` - Validate Profile Configuration

Validates profile configuration for errors.

```bash
poetry run gip config validate [profile-name|all]
```

**Arguments:**

- `profile-name` - Profile to validate (optional, defaults to active profile)
- `all` - Validate all profiles

**What it does:**

- Checks required fields are present
- Validates field formats (region, stack names, URLs)
- Verifies AWS credentials exist
- Reports validation errors with suggestions

**Examples:**

```bash
# Validate active profile
poetry run gip config validate

# Validate specific profile
poetry run gip config validate production

# Validate all profiles
poetry run gip config validate all
```

### `config export` - Export Profile Configuration

Exports a profile configuration to a file (sanitized).

```bash
poetry run gip config export [profile-name] [options]
```

**Arguments:**

- `profile-name` - Profile to export (optional, defaults to active profile)

**Options:**

- `--output <file>` - Output file path (default: `<profile-name>.json`)
- `--include-secrets` - Include sensitive values (not recommended)

**What it does:**

- Exports profile configuration to JSON file
- Removes sensitive values by default (client secrets)
- Creates portable configuration file

**Examples:**

```bash
# Export active profile (secrets removed)
poetry run gip config export

# Export specific profile to custom path
poetry run gip config export production --output prod-config.json

# Export with secrets (use caution)
poetry run gip config export --include-secrets
```

### `config import` - Import Profile Configuration

Imports a profile configuration from a file.

```bash
poetry run gip config import <file> [name]
```

**Arguments:**

- `file` - Path to configuration file (required)
- `name` - Name for imported profile (optional, uses name from file)

**Options:**

- `--overwrite` - Overwrite if profile already exists
- `--set-active` - Set as active profile after import

**What it does:**

- Imports profile configuration from JSON file
- Validates configuration before importing
- Creates new profile in `~/.gip/profiles/`
- Optionally sets as active profile

**Examples:**

```bash
# Import profile with default name
poetry run gip config import prod-config.json

# Import with custom name
poetry run gip config import config.json staging

# Import and set as active
poetry run gip config import config.json --set-active

# Overwrite existing profile
poetry run gip config import config.json production --overwrite
```

### `destroy` - Remove Infrastructure

Removes deployed AWS infrastructure.

```bash
poetry run gip destroy [stack] [options]
```

**Arguments:**

- `stack` - Specific stack to destroy: codebuild, analytics, quota, cowork-dashboard, dashboard, monitoring, distribution, networking, s3bucket, or auth (optional)

**Options:**

- `--profile <name>` - Configuration profile to use (uses active profile if not specified)
- `--force` - Skip confirmation prompts

**What it does:**

- Deletes CloudFormation stacks in reverse dependency order (codebuild → analytics → quota → cowork-dashboard → dashboard → monitoring → distribution → networking → s3bucket → auth), skipping any not enabled for the profile
- Shows resources to be deleted before proceeding
- Warns about manual cleanup requirements (e.g., CloudWatch LogGroups)

**Note:** Some resources like CloudWatch LogGroups may require manual deletion.

### `doctor` - Validate Installation Health

Runs health checks on the local machine to catch misconfigurations and aid troubleshooting.

```bash
poetry run gip doctor [options]
```

**Options:**

- `--verbose` / `-v` - Show raw JSON from `credential-process --explain` and `otel-helper --status`
- `--live` / `-l` - Also attempt authentication and check proxy connectivity
- `--json` - Machine-readable JSON output (for CI or support)
- `--profile <name>` - Check a specific profile

**Health Checks:**

| Check | What it validates |
|-------|-------------------|
| `credential-process` | Binary exists in install dir (.exe/.cmd/.ps1 on Windows) |
| `config.json` | Present, valid JSON, lists profiles |
| `aws-profile` | `~/.aws/config` references credential-process |
| `settings.json` | Claude Code settings file with env/hooks |
| `explain` | Calls `credential-process --explain` — shows resolved auth mode, provider, quota |
| `otel-helper` | Telemetry binary exists (only FAIL if monitoring configured) |
| `otel-status` | Calls `otel-helper --status` — proxy running? headers cached? |
| `auth-test` | (--live only) Attempts credential check |
| `proxy-health` | (--live only) TCP connect to OTEL proxy port |

**On failure:** Generates a pre-filled GitHub issue URL with diagnostics, environment, and auth mode.

### `models check` - Detect Model Catalog Drift

Compares the model catalog bundled in this release (`models.py`) against the live
Bedrock `ListFoundationModels` and `ListInferenceProfiles` APIs.

```bash
poetry run gip models check [options]
```

**Options:**

- `--region <region>` - Check a specific region instead of the profile's allowed Bedrock regions
- `--json` - Machine-readable JSON output (for cron/CI)
- `--propose` - Emit ready-to-paste catalog entries for models missing from the catalog
  (a `models.py` entry block plus an `extra_models` overlay JSON block)
- `--output <file>` - Write the `--propose` artifacts to a file in addition to stdout
  (requires `--propose`)

**Exit codes:** `0` = in sync, `1` = drift detected, `2` = check could not run
(missing/expired credentials, access denied, network).

Only read-only AWS calls are made: `bedrock:ListFoundationModels` and
`bedrock:ListInferenceProfiles`. No model invocation access is required.
Proposals are printouts only — the command never modifies any file on its own
(`--output` writes the proposal text to a new file, nothing else).

**Lifecycle warnings:** the report includes a `Model lifecycle` section for
catalog models observed `LEGACY` live, with the three lifecycle dates Bedrock
now publishes per model (`legacyTime`, `publicExtendedAccessTime`,
`endOfLifeTime`):

- *entered Legacy on `<date>`* — new customers can't use the model; existing
  access can lapse after 15 days of inactivity.
- *premium pricing begins/ACTIVE `<date>`* — during "public extended access"
  the model provider sets a pricing premium. The premium magnitude is **not**
  published in the API or docs — read the AWS Health Legacy notification.
- *inference FAILS after `<date>`* (critical, within 60 days of end of life) —
  after that date requests to the model fail permanently.

Lifecycle warnings are informational: they do not change the exit code (a
report can be "in sync" and still carry warnings). In `--json` output they
appear under `lifecycle_warnings`. To get *alerted* instead of having to run
the command, deploy the optional model-lifecycle stack (enable in
`gip init`, then `gip deploy model-lifecycle`); the rotation procedure is in
[RUNBOOKS.md — Model rotation](RUNBOOKS.md#9-model-rotation-legacy-premium-pricing-end-of-life).

See [Keeping the model catalog current](#keeping-the-model-catalog-current) for
what drift means and what to do about it.

### Go Binary Diagnostic Flags

These flags are available on the installed Go binaries (v2.5.0+):

```bash
# Print resolved configuration (no auth, no network)
credential-process --explain

# Print proxy and cache status
otel-helper --status

# Show version with commit SHA
credential-process --version   # → credential-process v2.5.0-beta.91 (62a232f)
```

`--explain` output includes: auth mode (oidc/idc/passthrough), provider type, federation type, quota config, storage mode, and file paths. Useful for verifying the binary detected the correct configuration before debugging auth failures.

## Keeping the model catalog current

The list of Claude models, CRIS inference profiles, and their region routing is
**hardcoded** in this repository (`source/governed_inference_platform/models.py`,
see REVIEW.md finding #13). When AWS launches a new Claude model or expands CRIS
routing, the catalog in your installed release does not know about it until the
repo is updated.

`gip models check` makes that drift visible:

```bash
# Check the active profile's allowed Bedrock regions
poetry run gip models check

# Check one geography explicitly (repeat per geography for full CRIS coverage)
poetry run gip models check --region eu-west-1

# Machine-readable, for automation
poetry run gip models check --json
```

**Cron example** — weekly drift check that emails the admin on drift:

```cron
0 9 * * 1 cd /opt/gip/source && poetry run gip models check --json > /var/log/gip-models-check.json || mail -s "gip model catalog drift detected" admin@example.com < /var/log/gip-models-check.json
```

**What drift means:**

- *New Anthropic models/profiles live but missing from the catalog* — the
  actionable case. A new model launched that this release cannot offer during
  `gip init`. Update the repo (or pull the latest release) to pick it up.
- *Catalog entries no longer available live* — a model or CRIS profile in the
  catalog is no longer returned by the live APIs in the checked regions
  (deprecation or access change).
- *CRIS destination-region drift* — a CRIS geography now routes to more (or
  fewer) destination regions than the catalog declares. This affects the
  region allow-lists baked into the IAM policies (`AllowedBedrockRegions`).
- Legacy on-demand-only models that were never in the curated catalog are
  reported as informational notes, not drift.

**Honest limitation:** this command only *detects* drift — it cannot fix it.
Adding a new model still requires a repo update, because tier preferences
(`MODEL_TIER_PREFERENCES`), pricing families, and display names in `models.py`
are curated by hand (REVIEW.md finding #13). Full dynamic catalog discovery at
runtime is future work; run `python scripts/validate_bedrock_regions.py` or
`gip models check` on a schedule in your own CI for the same comparison.

### Closing the gap: `--propose` and the `extra_models` overlay

Two paths shorten the time from "new model launched" to "users can select it"
(decision record: [ADR-0018](adr/0018-model-lifecycle-overlay-vs-codegen.md)):

**1. Codegen → repo PR (the system of record).** `gip models check --propose`
synthesizes a complete, ready-to-paste `_CLAUDE_MODELS_RAW` entry from the live
data the check already fetched — base model ID, per-geography CRIS profile IDs,
source and destination regions:

```bash
poetry run gip models check --propose --output proposals.txt
```

The snippet carries explicit `TODO(review)` markers for everything live data
cannot decide: tier placement in `MODEL_TIER_PREFERENCES`, rate limits, data
residency, and display naming. Proposals never self-place into tier chains —
a human reviews and merges. Source-region coverage is bounded by the regions
you checked; re-run with `--region` in each geography for full coverage
(partial coverage is flagged in the snippet).

**2. `extra_models` profile overlay (the additive escape hatch).** When you
can't wait for a release, paste the `extra_models` JSON block from `--propose`
into your profile in `~/.gip/config.json` (there is no wizard question —
it is a hand-edited field). Overlay entries become selectable in
`gip init`/`package` within minutes, with deliberately restricted semantics:

- **Additive only** — an entry that shadows a catalog key, base model ID, or
  CRIS profile ID fails validation loudly. The overlay can never change what
  the catalog says about an existing model.
- **Excluded from tier fallback chains** — `sonnet`/`opus`/`haiku`/`fable`
  tier resolution only ever reads the built-in catalog, so an unvetted overlay
  model can never be silently picked as a default.
- Display names are suffixed ` (overlay)` so they are visibly non-catalog.
- Default family rate limits apply; invalid overlays are reported by
  `gip init` (which falls back to the catalog rather than crashing).

The overlay is a bridge, not a fork: when the model lands in the repo catalog,
the overlay entry fails validation as a duplicate — delete it and re-run
`gip init`.
