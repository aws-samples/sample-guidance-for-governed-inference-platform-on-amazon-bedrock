# Agent-Assisted Deployment Playbook

This playbook is written to be executed by a coding agent (Claude Code,
OpenCode, Codex CLI, Kiro, etc.) under human supervision. Hand the agent this
file plus the four decision inputs below, and it can take a fresh AWS account
to a verified, governed Bedrock endpoint in about an hour.

**For the human supervisor:** the agent will pause at two checkpoints that
require values only you can provide (IdP registration, deployment choices) and
will show you verification output after every phase. Nothing here requires
giving the agent long-lived credentials — a normal admin session
(`aws sts get-caller-identity` works) is enough.

---

## Inputs the human must provide before starting

| Input | Example | Where it comes from |
|---|---|---|
| IdP provider + domain | `okta` / `company.okta.com` | Your identity team |
| OIDC client ID | `0oa1b2c3d4e5f6g7h8` | App registration (see checkpoint 1) |
| AWS region for infrastructure | `us-east-1` | Compliance/residency decision |
| Inference geography (CRIS) | `us`, `eu`, `apac`, `au`, `jp`, `global` | Compliance/residency decision |
| Monthly budget per user (USD) | `50` | Finance |

## Rules for the agent

1. **Never invent values** for the inputs above — stop and ask.
2. **Run the verification command after every phase** and show the human the
   output before proceeding. Do not continue past a failed gate.
3. **Prefer the answers-file path** (`gip init --from-file`) so the entire
   configuration is reviewable text before anything deploys.
4. All commands run from the repository root unless noted. The Python
   environment lives in `source/`.
5. If anything fails, run `gip doctor` first — it diagnoses and prints a
   pre-filled issue link — before attempting fixes.

---

## Phase 0 — Preconditions (agent, ~5 min)

Use Python 3.10 through 3.13. Linux administrators also need the `unzip`
package installed because distributed client bundles are ZIP archives.

```bash
git clone <this-repo> && cd <repo>
cd source && (poetry install || (uv venv --python 3.12 .venv && uv pip install -e .))
aws sts get-caller-identity          # admin session works
aws bedrock list-foundation-models --region <infra-region> \
  --query 'modelSummaries[?providerName==`Anthropic`] | length(@)'
```

**Gate:** caller identity resolves; Anthropic model count > 0. If the model
count is 0, STOP — the human must enable Anthropic model access in the
Bedrock console for every region the chosen CRIS profile spans (this cannot
be automated).

## Checkpoint 1 — IdP registration (human, ~10 min)

The human registers a **native/PKCE** OIDC app per the matching guide in
`assets/docs/providers/` with redirect URI `http://localhost:8400/callback`,
then gives the agent the domain and client ID. The agent must not proceed
with placeholder values.

## Phase 1 — Configuration as a reviewable file (agent, ~10 min)

Write `answers.yaml` from the inputs (schema documented in
`assets/docs/CLI_REFERENCE.md` → "Non-interactive / GitOps mode"; a complete
example is included there). Recommended baseline for a governed deployment:

- `quota.enabled: true`, `quota.limit_type: cost`,
  `quota.monthly_cost_limit: <budget>`
- monitoring enabled (central mode for fleets, sidecar for pilots)
- `RestrictToAnthropicModels` intent noted for the deploy phase
- inference geography per the human's residency decision

```bash
gip init --from-file answers.yaml --profile-name default
```

**Gate:** command exits 0; show the human the printed profile summary AND any
`[Data residency]` warning block. The human approves before phase 2.

## Phase 2 — Infrastructure (agent, ~20-30 min)

```bash
gip deploy --dry-run     # show the human what will be created
gip deploy               # auth → networking → monitoring → dashboard → quota ...
```

**Gate:** every stack reports CREATE_COMPLETE/UPDATE_COMPLETE. On failure:
`gip doctor`, read the stack events, fix, re-run (deploys are idempotent).

## Phase 3 — Client package (agent, ~5 min)

```bash
gip package --go                      # + `--harnesses all` for OpenCode/Aider/Pi configs
gip test                        # simulates a user install + real Bedrock call
```

**Gate:** `gip test` succeeds end-to-end (auth → STS → Bedrock
response). This is the moment the platform provably works.

## Phase 4 — Distribution (agent + human)

```bash
gip distribute            # presigned URLs (48h default), or landing page if configured
```

Show the human the URLs/next steps. For MDM fleets, hand over the generated
installer and (if Cowork is in scope) `gip cowork generate` artifacts.

## Phase 5 — Governance verification (agent, ~10 min)

Prove the guardrails to the human with three checks:

```bash
gip quota list                        # default cost policy present
gip models check                      # model catalog vs live Bedrock drift
```

1. **Blocking works:** create a throwaway policy —
   `gip quota set-user test@example.com --budget 0.01 --enforcement block` — then
   show that credential issuance is denied for that user (and remove the
   policy). This command requires fine-grained quota policies to be enabled;
   otherwise verify the deployed default policy instead.
2. **Telemetry works:** after the first real session, show the CloudWatch
   "Claude Code" dashboard populating (tokens, cost, cache savings widgets).
3. **Metering (if enabled):** after ~30 minutes of use, show the
   `GIP/Quota MeteringDrift` metric exists — client and server usage
   records are being compared.

---

## What the agent must leave behind

1. `answers.yaml` committed/stored — the deployment is now reproducible.
2. `gip init --export-answers verify.yaml` output diffed against
   `answers.yaml` (round-trip check).
3. A short handover note: stack names, dashboard URL, distribution URL,
   quota defaults, and the two operational docs the admin will need first:
   [RUNBOOKS.md](RUNBOOKS.md) and [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
