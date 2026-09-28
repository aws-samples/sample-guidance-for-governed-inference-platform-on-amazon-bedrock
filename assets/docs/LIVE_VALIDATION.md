# Live Validation Runbook

Several decisions in this platform rest on documented but **unverified**
assumptions about live AWS behavior (each is flagged `unverified` in its
owning doc or ADR). This runbook turns every one of those assumptions into an
operator-executable validation: what to run, what PASS and FAIL look like, and
which document gets its `unverified` flag lifted on a dated pass. The decision
to defer these validations rather than block release is recorded in
[ADR-0031](adr/0031-defer-live-aws-validation.md).

Run these against a deployment you are authorized to mutate — a sandbox
account or a staging copy of your deployment, never blind against production.
Items are independent unless a precondition says otherwise; the
[terminal sweep](#lv-17-validation-fixture-sweep) always runs last.

Conventions:

- `<...>` placeholders are yours to fill (stack names default to
  `<identity-pool-name>-<stack-type>`, e.g. `myorg-metering`).
- Every mutation names its cleanup. Tag every fixture you create with a
  sweepable marker, e.g. `Key=gip-validation,Value=live` — LV-17 asserts on it.
- Cost figures are order-of-magnitude at the time of writing; each item states
  its own.
- Auth mode matters: several items are OIDC-only (the gateway authorizer is
  `CUSTOM_JWT`). Each item states which modes it applies to.

Related: [RUNBOOKS.md](RUNBOOKS.md) (operational procedures),
[FAILURE_POSTURE.md](FAILURE_POSTURE.md) (the failure matrix LV-6/LV-7
exercise), [QUOTA_MONITORING.md](QUOTA_MONITORING.md),
[COST_ATTRIBUTION.md](COST_ATTRIBUTION.md).

## Status

| # | Validation | Risk if the assumption is wrong | Status | Last verified |
|---|---|---|---|---|
| [LV-1](#lv-1-aip-cost-allocation-tag-flow-into-cost-explorer-and-cur) | AIP cost-allocation tag flow into Cost Explorer/CUR | Team cost attribution silently never materializes in billing data | **blocked** (payer-only tag activation) | 2026-08-14 |
| [LV-2](#lv-2-aip-identifier-in-the-cloudwatch-modelid-dimension) | AIP identifier in the CloudWatch `ModelId` dimension | Per-team CloudWatch dashboards impossible from `AWS/Bedrock` metrics | **verified** | 2026-07-14 |
| [LV-3](#lv-3-packaged-claude-code-inference-end-to-end-with-aip-pins) | Packaged Claude Code inference end-to-end with AIP pins | Distributed settings ship a model pin Bedrock rejects | **verified** | 2026-08-14 |
| [LV-4](#lv-4-mantle-tripwire-alarm-fires-and-recovers) | Mantle tripwire alarm fires and recovers | The shipped ungoverned-use tripwire is dead weight | **verified** | 2026-08-14 |
| [LV-5](#lv-5-cache-token-metering-fidelity) | Cache-token metering fidelity in invocation logs | Strict metering mode stays blocked; server figures under-count the majority of Claude Code tokens | **verified, resolved-positive** | 2026-08-14 |
| [LV-6](#lv-6-collector-outage-chaos-central-mode) | Collector-outage chaos (central mode) | FAILURE_POSTURE collector row is wrong; outage behavior differs from the documented posture | **verified** | 2026-08-14 |
| [LV-7](#lv-7-quota-api-outage-chaos) | Quota-API-outage chaos | Silent fail-open: credentials issued while the quota API is down in `closed` mode | **verified** | 2026-08-14 |
| [LV-8](#lv-8-cedar-array-claim-representation-per-idp) | Cedar array-claim representation per IdP | `ENFORCE` locks out entitled users; entitlement is never safely enableable | **resolved-negative (Cognito only)** | 2026-08-14 |
| [LV-9](#lv-9-memory-gateway-to-lambda-header-propagation-five-item-checklist) | Memory: gateway→Lambda header propagation (five-item checklist) | User-memory tools remain permanently inert behind the deploy gate | **resolved-negative** | 2026-08-14 |
| [LV-10](#lv-10-sessionnamebinding-live-sts-behavior-per-provider) | `SessionNameBinding` live STS behavior per provider | Enabling binding locks out legitimate users (fail-closed) or fails to bind | **verified (Cognito only)** | 2026-08-14 |
| [LV-11](#lv-11-oidc-aud-trust-condition-live-token-exchange-per-idp) | OIDC `:aud` trust condition — live token exchange per IdP | Trust-policy audience pin breaks federation for a provider whose token `aud` differs | **verified (Cognito only)** | 2026-08-14 |
| [LV-12](#lv-12-bearer-token-inference-through-a-bedrock-runtime-vpc-endpoint) | Bearer-token inference through a `bedrock-runtime` VPC endpoint | Network-isolation guidance overpromises private connectivity for API-key callers | **verified** | 2026-08-14 |
| [LV-13](#lv-13-claude-desktop-contract-ownership) | Claude Desktop contract ownership | A local implementation drifts from Claude Apps Gateway | **superseded — exact upstream source mirrored** | 2026-08-15 |
| [LV-14](#lv-14-collector-https-and-claude-desktop-internal-alb-restriction) | Collector HTTPS + Claude Desktop internal-ALB restriction, live | Template assertions don't hold at deploy time, or the shared-token path is reachable where it must not be | **verified** | 2026-08-14 |
| [LV-15](#lv-15-generated-harness-configs-against-live-clis) | Generated harness configs against live CLIs | Shipped OpenCode/Codex/Claude Code MCP wiring fails on first customer use | **verified with model caveats** | 2026-08-14 |
| [LV-16](#lv-16-web-search-and-memory-outside-us-east-1) | Web Search / Memory deployment outside us-east-1 | Regional availability docs are stale in either direction | **verified, web search rejected in us-west-2** | 2026-08-14 |
| [LV-17](#lv-17-validation-fixture-sweep) | Validation-fixture sweep (terminal) | Orphaned fixtures keep billing and pollute the account | **blocked by EC2 Mac 24-hour floor** | 2026-08-15 |

When an item passes, update its Status to `verified`, fill Last verified, and
make the doc edits listed in that item's **On pass** row.

## 2026-08-14 validation environment and baseline

The run used a fresh sandbox child account with Cognito OIDC in `us-east-1`.
The account identifier is intentionally not published. All 14 platform stacks
deployed or updated successfully after the fixes. `gip test` passed all six
checks: OIDC, quota, STS email-bound session, Bedrock, inference profiles, and
the quota API.

The presigned ZIP was downloaded, installed, authenticated through a real
browser flow, and used for a Claude Code prompt that returned `4` on Ubuntu
24.04, Windows Server 2022 as a standard user, and EC2 Mac running macOS
Sequoia on arm64. Windows validation used a scheduled-task fallback because an
SSM service session cannot use `Start-Process -Credential`; this is a harness
constraint, separate from the product installer fix. On headless macOS,
keyring mode issued credentials on first authentication, but a second read
requires native Keychain consent UI. Keyring mode is therefore not recorded as
fully passed on macOS.

A second full run on 2026-08-15 redeployed all 14 stacks from committed source
without hand patches and passed `gip test` 6/6 both before and after the full
matrix. Generated Linux, Windows, and macOS packages installed without product
workarounds; OpenCode, Aider, Codex-negative, and AgentCore MCP coverage
repeated successfully. macOS `gip cleanup` removed all manifest-owned files and
the generated AWS profile. LV-2 through LV-16 reproduced their expected
positive or resolved-negative outcomes. Teardown is complete except for one
EC2 Mac host under AWS's mandatory 24-hour allocation floor.

---

## LV-1: AIP cost-allocation tag flow into Cost Explorer and CUR

| | |
|---|---|
| **Claim** | Tags on an Application Inference Profile flow to Cost Explorer and CUR, and the same CUR line item also carries the caller's session name in `line_item_iam_principal` ([COST_ATTRIBUTION.md §5](COST_ATTRIBUTION.md#5-optional-application-inference-profiles-for-teamcost-center-attribution); [ADR-0021](adr/0021-application-inference-profiles-additive.md) records live verification as a residual item — tag activation lags and is not retroactive). |
| **Applies to** | All auth modes (the AIP is invoked with any governed credentials). |
| **Preconditions** | Permission to run `ce:UpdateCostAllocationTagsStatus` — in AWS Organizations this may require management-account access for a linked account; confirm before starting. For the coexistence half: a CUR 2.0 (Data Exports) export with IAM principal data enabled, queryable via Athena. No platform stacks required. |
| **Cost** | A few Converse calls (cents) + Cost Explorer API calls ($0.01 each). Wall clock: tags take up to 24 h to appear and a further 24 h to activate — plan for a 2-day soak. |

### Procedure

```bash
# 1. Create a team-tagged AIP from a geographic CRIS source (not global.*)
aws bedrock create-inference-profile --region <region> \
  --inference-profile-name gip-validation-team-<team> \
  --model-source 'copyFrom=arn:aws:bedrock:<region>:<account-id>:inference-profile/us.anthropic.claude-sonnet-4-5-20250929-v1:0' \
  --tags key=team,value=<team> key=gip-validation,value=live
# Note the returned inferenceProfileArn as $AIP_ARN.

# 2. Invoke through it (3x for signal)
aws bedrock-runtime converse --region <region> --model-id "$AIP_ARN" \
  --messages '[{"role":"user","content":[{"text":"Reply with the single word: pong"}]}]' \
  --query usage

# 3. Activate the cost-allocation tag (retry the next day on NotFound —
#    the tag must first appear in billing before it can be activated)
aws ce update-cost-allocation-tags-status \
  --cost-allocation-tags-status TagKey=team,Status=Active

# 4. AFTER activation confirms Active, invoke again (activation is not
#    retroactive — only post-activation usage carries the tag)
aws ce list-cost-allocation-tags --status Active

# 5. Day +1/+2 readback
aws ce get-cost-and-usage \
  --time-period Start=<post-activation-date>,End=<today> \
  --granularity DAILY --metrics UnblendedCost \
  --filter '{"Tags":{"Key":"team","Values":["<team>"]}}'
```

For the coexistence half, query the CUR 2.0 export in Athena for the same
window and confirm the Bedrock line items carry **both** the `team` resource
tag and the caller identity in `line_item_iam_principal`. Record the exact
`resource_tags` map key format you observe — it is not documented and
COST_ATTRIBUTION.md needs it.

### PASS / FAIL

- **PASS:** step 5 returns tagged spend without Athena, and the Athena query
  shows tag + `line_item_iam_principal` on the same line item.
- **FAIL:** the tag never appears 48 h after post-activation usage (tag flow
  broken — COST_ATTRIBUTION.md §5's Cost Explorer claim must be corrected), or
  tags appear but the principal column is empty (the coexistence claim from
  the granular-cost-attribution launch does not hold for AIPs — correct §5).

### Cleanup

```bash
aws bedrock delete-inference-profile --region <region> \
  --inference-profile-identifier "$AIP_ARN"
```

Deactivating the `team` cost-allocation tag is optional (no ongoing cost);
note the decision in the LV-17 sweep either way.

**On pass:** record the observed `resource_tags` key format in
COST_ATTRIBUTION.md §5, close ADR-0021's residual verification item with the
date, and update the status table above.

---

## LV-2: AIP identifier in the CloudWatch `ModelId` dimension

| | |
|---|---|
| **Claim** | Invocations made through an AIP appear in `AWS/Bedrock` metrics under a `ModelId` dimension value that identifies the profile (needed for per-team CloudWatch dashboards). Invocation *logs* were already known to carry the full AIP ARN in `modelId`; this item covers the metrics plane. |
| **Status** | **Verified 2026-07-14** (read-only survey): datapoints for an AIP appeared under a `ModelId` dimension carrying the profile's **opaque identifier** (the trailing ID segment of the profile ARN), not the full ARN and not the underlying foundation-model ID. |
| **Preconditions** | Any AIP invocation inside the trailing 2-week `list-metrics` window (one LV-1 Converse call refreshes it). |
| **Cost** | Read-only; cents of CloudWatch API calls. |

### Procedure (re-verification)

```bash
aws cloudwatch list-metrics --region <region> \
  --namespace AWS/Bedrock --metric-name Invocations
# Find the ModelId dimension value matching your AIP's identifier, then:
aws cloudwatch get-metric-statistics --region <region> \
  --namespace AWS/Bedrock --metric-name Invocations \
  --dimensions Name=ModelId,Value=<observed-value> \
  --start-time <T-24h> --end-time <now> --period 3600 --statistics Sum
```

- **PASS:** datapoints attach to a dimension value identifying the profile.
- **FAIL:** AIP invocations appear only under the underlying foundation-model
  ID — per-team dashboards from `AWS/Bedrock` metrics are then impossible;
  document that instead. Either observation is a documentable outcome.

**On pass (doc update still pending):** COST_ATTRIBUTION.md §5 currently
documents only the invocation-log `modelId`; add a note that the CloudWatch
`ModelId` dimension carries the opaque profile identifier.

---

## LV-3: Packaged Claude Code inference end-to-end with AIP pins

| | |
|---|---|
| **Claim** | `gip package` writes AIP ARNs into the distributed Claude Code settings (`ANTHROPIC_DEFAULT_SONNET_MODEL` etc.), and Claude Code accepts the ARN as a model identifier end to end ([ADR-0021](adr/0021-application-inference-profiles-additive.md) decision 1; the packaged pin has never carried a live inference call). |
| **Applies to** | OIDC or Cognito (a deployed auth stack is required for credential-process). |
| **Preconditions** | Auth stack deployed (`gip deploy auth`, default name `<identity-pool-name>-stack`). A live AIP ARN (LV-1's fixture works). Claude Code installed on the executing machine. Because AIP IDs are opaque, explicitly set `restrict_to_anthropic_models: false` for the approved AIP deployment and redeploy auth; the default `true` correctly denies all opaque AIPs. |
| **Cost** | One inference call (cents) if the auth stack already exists. |

Live-verified on 2026-08-14 with an approved Sonnet 4.5 AIP and the explicit
opaque-AIP model-restriction opt-out: the fresh-user package returned `pong`,
and the invocation log `modelId` was the full application-inference-profile
ARN. The auth restriction was restored and the fixture deleted afterward.

### Procedure

1. Set the profile field `inference_profile_sonnet_arn` to the AIP ARN
   (re-run `gip init`, or edit `~/.gip/profiles/<profile>.json`). Record the
   explicit governance decision `restrict_to_anthropic_models: false`, then
   run `gip deploy auth`; opaque AIP IDs cannot be server-side classified as
   Anthropic by the default IAM resource pattern.
2. `poetry run gip package`
3. Inspect the output: `gip-settings/settings.json` must contain
   `env.ANTHROPIC_DEFAULT_SONNET_MODEL` = the AIP ARN, while
   `ANTHROPIC_MODEL` (if present) stays an alias such as `sonnet`.
4. Install the package on a test machine and run:

   ```bash
   claude -p "Reply with the single word: pong"
   ```

5. Confirm server-side attribution: the invocation appears in Bedrock
   invocation logs with `modelId` = the AIP ARN (log group
   `/aws/bedrock/gip-metering` if the metering stack is deployed), or the
   LV-2 metric dimension increments.

### PASS / FAIL

- **PASS:** steps 3–5 all hold — settings correct, response returned,
  attribution lands on the AIP.
- **FAIL:** settings are correct but Bedrock rejects the ARN through the
  harness, or attribution lands on the underlying model ID. Either failure
  means ADR-0021's Claude Code claim needs a caveat before customers rely
  on AIP attribution.

### Cleanup

Remove the installed settings from the test machine; the AIP is deleted in
LV-1 cleanup.

**On pass:** note the dated end-to-end proof in COST_ATTRIBUTION.md §5 and
the status table above.

---

## LV-4: Mantle tripwire alarm fires and recovers

| | |
|---|---|
| **Claim** | The metering stack's `MantleEndpointUsageAlarm` (physical name `<metering-stack>-mantle-endpoint-usage`) transitions to `ALARM` on any `bedrock-mantle` inference in the account/region and returns to `OK` when it stops ([ADR-0022](adr/0022-mantle-metering-deferred-tripwire.md)). Live-verified in `us-east-1` on 2026-08-14: `OK` to `ALARM` to `OK`. |
| **Applies to** | All auth modes (the alarm is account/region-scoped). The test call must use **ungoverned** credentials: every governed managed policy carries `DenyBedrockMantleEndpoint`, so use your admin/deployment credentials. |
| **Preconditions** | Metering stack deployed (`gip deploy metering`, default name `<identity-pool-name>-metering`; requires quota monitoring enabled). Optional: pass `AlarmTopicArn` so you also verify the notification path. |
| **Cost** | One Mantle call (~100 tokens, cents); the alarm itself ≈ $0.10/month prorated. ~1 h wall clock (two alarm windows). |

### Procedure

```bash
# 1. Baseline: alarm exists and is OK / INSUFFICIENT_DATA
aws cloudwatch describe-alarms --region <region> \
  --alarm-names <metering-stack>-mantle-endpoint-usage

# 2. One controlled Mantle call with ADMIN (ungoverned) credentials.
#    Use an OpenAI-family model: Claude models on Mantle reject the
#    chat-completions route.
curl --aws-sigv4 "aws:amz:<region>:bedrock-mantle" \
  --user "$AWS_ACCESS_KEY_ID:$AWS_SECRET_ACCESS_KEY" \
  -H "x-amz-security-token: $AWS_SESSION_TOKEN" \
  -H "Content-Type: application/json" \
  "https://bedrock-mantle.<region>.api.aws/v1/chat/completions" \
  -d '{"model":"openai.gpt-oss-20b","messages":[{"role":"user","content":"Reply with the single word: pong"}],"max_tokens":8}'

# 3. Poll every ~2 min for up to 15 min
aws cloudwatch describe-alarm-history --region <region> \
  --alarm-name <metering-stack>-mantle-endpoint-usage --max-items 5

# 4. Make NO further Mantle calls; keep polling ~15 more min for recovery
```

### PASS / FAIL

- **PASS:** `ALARM` within two 5-minute evaluation windows of the call
  (alarm: period 300 s, 1 evaluation period, threshold ≥ 1), then `OK`
  within two windows of stopping (`TreatMissingData: notBreaching`). If a
  topic was wired, the SNS notification arrived.
- **FAIL:** no transition — the Metrics Insights expression or the
  `AWS/BedrockMantle` namespace assumption is wrong and the shipped tripwire
  is dead weight. Fix `deployment/infrastructure/quota-metering.yaml` before
  relying on the tripwire claim in [GUARDRAILS.md](GUARDRAILS.md) and
  QUOTA_MONITORING.md.

### Cleanup

None beyond LV-17 if the metering stack stays; if it was deployed only for
this test, `gip destroy metering` — then verify the account invocation-logging
configuration matches your pre-test snapshot (the stack's custom resource
creates or adopts the account-level logging config; take the snapshot per
LV-5 step 0 before deploying).

**On pass:** update ADR-0022 (tripwire live-fired, date) and the
QUOTA_MONITORING.md tripwire section.

---

## LV-5: Cache-token metering fidelity

| | |
|---|---|
| **Claim** | Open question Q1 of [ADR-0005](adr/0005-server-side-metering-from-invocation-logs.md): whether cache read/write token counts survive **metadata-only** invocation logging and match the client-visible `usage` block. Cache-write is typically the majority of Claude Code tokens; strict metering mode is blocked until this is answered empirically. |
| **Applies to** | All auth modes (direct Bedrock API calls). |
| **Preconditions** | **Snapshot the account invocation-logging config first** — `put-model-invocation-logging-configuration` overwrites a per-account, per-region singleton. If the metering stack is deployed, use its log group (`/aws/bedrock/gip-metering`) and skip step 1. Pick a cache-capable Claude model (or the LV-1 AIP ARN, which doubles as AIP soak data). |
| **Cost** | ~14k input tokens ≈ cents. |

### Procedure

```bash
# 0. SNAPSHOT — restore this exactly when done
aws bedrock get-model-invocation-logging-configuration --region <region>

# 1. Only if no metering stack: enable metadata-only logging to your own
#    log group + delivery role (all five content-delivery flags false)
aws bedrock put-model-invocation-logging-configuration --region <region> \
  --logging-config '{"cloudWatchConfig":{"logGroupName":"<log-group>","roleArn":"<bedrock-logging-role-arn>"},"textDataDeliveryEnabled":false,"imageDataDeliveryEnabled":false,"embeddingDataDeliveryEnabled":false,"videoDataDeliveryEnabled":false,"audioDataDeliveryEnabled":false}'

# 2. Build two near-identical large messages sharing a cachePoint
python3 - <<'EOF'
import json
prefix = "You are a metering reconciliation test. " + " ".join(
    f"Fact {i}: the quick brown fox jumps over the lazy dog number {i}." for i in range(400))
msg = {"role":"user","content":[{"text":prefix},{"cachePoint":{"type":"default"}},{"text":"Reply with the single word: ONE"}]}
json.dump([msg], open("/tmp/cache-msg1.json","w"))
msg["content"][-1] = {"text":"Reply with the single word: TWO"}
json.dump([msg], open("/tmp/cache-msg2.json","w"))
EOF

# 3. Request 1 (cache write), request 2 (cache read) — capture BOTH usage blocks
T0=$(($(date +%s)*1000))
aws bedrock-runtime converse --region <region> --model-id <model-or-aip-arn> \
  --messages file:///tmp/cache-msg1.json --query 'usage'
aws bedrock-runtime converse --region <region> --model-id <model-or-aip-arn> \
  --messages file:///tmp/cache-msg2.json --query 'usage'

# 4. Read the invocation-log records
sleep 60
aws logs filter-log-events --region <region> \
  --log-group-name <log-group-from-step-1-or-/aws/bedrock/gip-metering> \
  --start-time "$T0" --query 'events[].message' --output text
```

Match records to requests by `requestId`. Compare the client `usage` blocks
against every log key matching `cache.*token` (documented spelling families:
`cacheReadInputTokens`/`cacheWriteInputTokens` and
`cache_read_input_tokens`/`cache_creation_input_tokens`), plus
`input.inputTokenCount`, `output.outputTokenCount`, and `modelId`.

### Decision matrix (any row selected with evidence is a PASS for this item)

| Observation | Disposition |
|---|---|
| Cache fields present in metadata-only records, values match client `usage` (±1 token) | Q1 resolved-positive; the cache prerequisite for strict mode is satisfied. |
| Cache fields absent from metadata-only records | Q1 resolved-negative; strict mode would need a conscious, documented waiver to enforce input+output only. |
| Fields present but inconsistent with client `usage` | Keep raw-capture logging, extend the shadow soak, re-run after one model-release cycle. |

**FAIL** is only "no evidence collected."

### Follow-on (optional, metering stack deployed)

Re-run the two requests and confirm `server_cache_read_tokens` /
`server_cache_write_tokens` accrue on the user's `UserQuotaMetrics` item —
end-to-end rather than log-level proof.

### Cleanup

**Restore the step-0 snapshot exactly** (or delete the configuration if none
existed). Remove any log group/role you created (fold into LV-17).

**On pass:** update ADR-0005's status line and the strict-mode prerequisite in
QUOTA_MONITORING.md with the selected matrix row and date.

---

## LV-6: Collector-outage chaos (central mode)

| | |
|---|---|
| **Claim** | The collector-outage row of [FAILURE_POSTURE.md](FAILURE_POSTURE.md): inference and credentials unaffected, client OTLP exports fail without crashing sessions, the `<monitoring-stack>-collector-unhealthy` alarm fires and recovers, the telemetry gap is permanent, and quota checks silently keep returning `allowed` from stale counts. Live-verified on 2026-08-14 by scaling 1 to 0 to 1: Claude Code and quota survived, alarm `ALARM` then `OK`, missing datapoints remained permanent, and metrics resumed. |
| **Applies to** | OIDC/Cognito central-monitoring deployments. |
| **Preconditions** | Central-mode monitoring stack (`gip deploy monitoring`, default name `<identity-pool-name>-otel-collector`) + a packaged client emitting telemetry, with a pre-kill baseline showing metrics flowing. |
| **Cost** | Existing Fargate + ALB hours (≈ $0.05/hr combined); a half-day game-day ≈ $1–2 if the stack is deployed for the test. |

### Procedure

```bash
# 1. Resolve the CFN-generated service name
aws ecs list-services --cluster gip-otel-cluster --region <region>

# 2. Kill
aws ecs update-service --cluster gip-otel-cluster --region <region> \
  --service <service> --desired-count 0

# 3. During the outage: run `claude -p` sessions; capture client behavior.
aws cloudwatch describe-alarms --region <region> \
  --alarm-names <monitoring-stack>-collector-unhealthy

# 4. Recover
aws ecs update-service --cluster gip-otel-cluster --region <region> \
  --service <service> --desired-count 1

# 5. Post: confirm metrics resume and the outage window stays empty
```

Also verify the documented **silent enforcement gap** during the outage: a
credential refresh still succeeds and `gip-quota-check` returns `allowed`
from the last recorded usage (nothing errors, so neither fail-mode triggers).

### PASS / FAIL

- **PASS:** all five behaviors observed and timestamped — sessions survive,
  inference unaffected, alarm `ALARM` then `OK` (ALB `HealthyHostCount < 1`
  for 3 minutes, `TreatMissingData: breaching`), permanent dashboard gap,
  quota silently allows.
- **FAIL:** a client session breaks, the alarm never fires, or it fails to
  recover — each contradicts a FAILURE_POSTURE.md row and must be fixed in
  doc or code before the posture claim is repeated.

### Cleanup

Step 4 is the recovery. Delete the stack in LV-17 if created for this test.

**On pass:** replace the "chaos verification … has not yet been executed"
caveat at the end of FAILURE_POSTURE.md with dated evidence (this item plus
LV-7).

---

## LV-7: Quota-API-outage chaos

| | |
|---|---|
| **Claim** | Quota rows 1–2 of [FAILURE_POSTURE.md](FAILURE_POSTURE.md): with the quota API unreachable, credential issuance **denies** (`quota_fail_mode` default `closed`), with an actionable stderr reason, never a silent skip; `quota_fail_mode: "open"` is the break-glass. Live-verified on 2026-08-14: closed denied on 503, open allowed, and closed recovered after restore. |
| **Applies to** | OIDC (quota-enabled deployments). |
| **Preconditions** | Quota stack (`gip deploy quota`, default `<identity-pool-name>-quota`) + a packaged client with the quota endpoint configured and a valid login. |
| **Cost** | Negligible — the reserved-concurrency toggle is instant and fully reversible; no data touched. |

### Procedure

```bash
# 1. Baseline: a fresh credential-process run succeeds
credential-process --profile <profile> --clear-cache
credential-process --profile <profile>

# 2. Kill: throttle the quota-check Lambda to zero
aws lambda put-function-concurrency --region <region> \
  --function-name gip-quota-check --reserved-concurrent-executions 0

# 3. Clear the credential cache and force a fresh check
credential-process --profile <profile> --clear-cache
credential-process --profile <profile>          # EXPECT: deny, non-zero exit

# 4. Break-glass: set "quota_fail_mode": "open" in the client profile
#    config, re-run — EXPECT: allow with a warning

# 5. Recover
aws lambda delete-function-concurrency --region <region> \
  --function-name gip-quota-check
```

Optional, time-boxed: with `closed` restored and valid cached credentials,
confirm cached credentials keep working until the 30-minute re-check, and
that a re-check failure in `closed` mode denies **and expires the cache**.

### PASS / FAIL

- **PASS:** step 3 denies with a clear stderr reason
  (`connection_error`/`api_error`), step 4 allows with a warning, step 5
  restores normal issuance.
- **FAIL:** credentials are issued while the API is down in `closed` mode —
  a silent fail-open and a release blocker; or the client is left in an
  unrecoverable state after restore.

### Cleanup

Step 5 restores concurrency; revert `quota_fail_mode` to `closed` in the
client profile.

**On pass:** same FAILURE_POSTURE.md caveat replacement as LV-6.

---

## LV-8: Cedar array-claim representation per IdP

| | |
|---|---|
| **Claim** | The gateway entitlement policy assumes JWT **array** claims (`groups` / `cognito:groups`) surface to Cedar as sets, so `containsAny` matches ([ADR-0014](adr/0014-gateway-cedar-entitlements.md) live-verification caveat: the representation is undocumented; if arrays flatten to strings the permit never matches and `ENFORCE` would lock out entitled users — which is why `LOG_ONLY` is the default and `ValidationMode: IGNORE_ALL_FINDINGS` is set). |
| **Applies to** | OIDC only (Cedar principals are JWT-derived; IDC is a no-op by design). |
| **Preconditions** | Web search stack deployed with `websearch_entitled_groups` set and `websearch_policy_mode: LOG_ONLY` (`gip deploy websearch`, default `<identity-pool-name>-websearch`). Two test users: one in an entitled group, one not. |
| **Cost** | $7 / 1,000 search queries + $0.000025 per authorization decision — a handful of calls, cents. |

**2026-08-14 result:** resolved-negative for Cognito. AgentCore exposes both
`groups` and `cognito:groups` as `String`, so the `containsAny` policy cannot be
created. The product rejects non-empty entitled groups and `ENFORCE` pending
redesign. Okta remains untested.

### Procedure

For each IdP you deploy against (the representation is verified **per
deployment**, not guaranteed across providers):

```bash
# 1. Bearer for the entitled user (never opens a browser; sign in once first)
credential-process --profile <profile> --get-mcp-auth-header

# 2. One tool call through the gateway MCP endpoint per user
curl -sS "<GatewayMcpEndpoint>" \
  -H "Authorization: Bearer <id_token>" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
# then a tools/call with a trivial search query for each user
```

3. Read the gateway's Cedar decision traces for both calls and confirm:
   entitled-group **ALLOW**, non-entitled **DENY**. `[placeholder: the exact
   trace location is the AgentCore gateway's policy decision logging — pin
   the log group/console path for your deployment when you first run this;
   the repo does not pin it]`
4. Forged-audience probe: a token minted for a different client ID must be
   rejected with 401 at the authorizer (never reaching Cedar).
5. IDC-mode deployments: confirm the stack update is a no-op (no policy
   engine attached).

### PASS / FAIL

- **PASS after redesign:** all four checks hold for the replacement policy and
  the deployed IdP before enforcement is reintroduced.
- **FAIL:** the entitled user shows DENY in LOG_ONLY — the array claim is
  flattening to a string for this IdP. Do **not** enable `ENFORCE`; record
  the observed representation in ADR-0014 and fix the policy first.

**On pass:** update ADR-0014's live-verification caveat with provider + date,
and WEB_SEARCH.md's staged-rollout note.

---

## LV-9: Memory gateway-to-Lambda header propagation (five-item checklist)

| | |
|---|---|
| **Claim** | Whether the gateway can forward the validated `Authorization` header to the memory Lambda target. Resolved-negative on 2026-08-14: AgentCore rejects `Authorization` in `MetadataConfiguration.AllowedRequestHeaders`. Memory remains deployable log-only; active identity-dependent tools are rejected pending redesign. |
| **Applies to** | OIDC only. |
| **Preconditions** | Web search + memory stacks deployed (`gip deploy websearch`, then `gip deploy memory`; memory default name `<identity-pool-name>-memory`). Two test users (one is the "victim" in the forged-identity probe). |
| **Cost** | AgentCore Memory usage-billed: short-term events $0.25/1k, retrievals $0.50/1k — cents for this checklist. |

### Procedure

This checklist is retained as the acceptance contract for a future identity
redesign. The current product does not permit flipping `DeployGate` to active:

1. Header propagation: with a valid JWT, the Lambda logs show the forwarded
   Authorization value (client_context.custom or event headers).
2. Derived actorId equals `email:<sha256(lowercase-email)>` for the JWT email
   the gateway validated (raw email punctuation is not a valid AgentCore
   Memory actorId).
3. Forged-identity probe: `actor_id=victim@…` in args with alice's token
   touches only alice's namespaces.
4. Extraction timing supports the 24h sweep horizon.
5. Second-target lifecycle: create/delete leaves web search intact.

Execution notes per item:

- Items 1–3: call `memory_store` / `memory_retrieve` through the gateway MCP
  endpoint with a real bearer (same `curl` shape as LV-8, `tools/call`), then
  read the tools Lambda's logs:

  ```bash
  aws logs tail /aws/lambda/<memory-stack>-memory-tools --since 15m --region <region>
  ```

  In `log-only` mode the tools return a gated response while still logging —
  items 1–2 are verifiable **before** activating the gate.
- Item 3: include an `actor_id` argument naming the second user in the tool
  call; confirm via `gip memory forget-user <victim-email> --dry-run` that
  the victim's namespaces contain nothing from the probe.
- Item 4: store a fact, then poll `memory_retrieve` (or the data plane) until
  the extracted record appears; it must be well inside 24 h.
- Item 5: `gip destroy memory`, then confirm web search still answers
  (`tools/list` via LV-8 step 2), then redeploy memory.

### PASS / FAIL

- **PASS after redesign:** all five items hold before active mode is
  reintroduced.
- **FAIL on item 1:** the header never reaches the Lambda — the documented
  fallback is a Lambda REQUEST interceptor (ADR-0016 alternatives); keep the
  gate at `log-only` and file the change. FAIL on item 3 is a security stop:
  do not activate the gate.

**On pass:** update ADR-0016 (checklist executed, date) and the MEMORY.md
status callout.

---

## LV-10: SessionNameBinding live STS behavior per provider

| | |
|---|---|
| **Claim** | With `SessionNameBinding=email|sub`, STS resolves the trust-policy variable `${<provider-host>:email|sub}` from the IdP-signed token and rejects any `AssumeRoleWithWebIdentity` whose session name is not exactly the claim ([ADR-0015](adr/0015-session-name-binding.md)). Cognito `email` mode was live-verified on 2026-08-14; other providers, `sub`, and GovCloud remain open. |
| **Applies to** | Direct STS OIDC mode (`FederationType=direct`). |
| **Preconditions** | Auth stack deployed with `SessionNameBinding=email` (set `session_name_binding` in the profile, `gip deploy auth`) and the updated client on the canary machine. Do **not** use `sub` mode until the whole fleet runs the updated client — fail-closed lockout by design. |
| **Cost** | Free (STS calls). |

### Procedure

Per provider you deploy (Okta, Entra ID, Auth0, Google, generic, Cognito
user pool):

```bash
# 0. Auth0 only — verify the stored IAM provider name has no trailing slash
#    (the trust-policy condition-key prefix assumes it doesn't):
aws iam list-open-id-connect-providers

# 1. Positive: a bound assume through the normal client path
credential-process --profile <profile> --clear-cache
credential-process --profile <profile>          # EXPECT: credentials issued

# 2. Negative (forged session name): replay the same id_token manually
aws sts assume-role-with-web-identity \
  --role-arn <DirectIAMRole-arn> \
  --role-session-name someone-else@example.com \
  --web-identity-token "<id_token>"             # EXPECT: AccessDenied
```

3. Check CloudTrail for the denied `AssumeRoleWithWebIdentity` event to
   confirm the deny came from the trust-policy condition.
4. Entra ID: repeat step 1 with a user/tenant configuration known to omit the
   `email` claim if you have one — confirm the client fails **closed** with an
   actionable error (this is the documented behavior, and the reason `sub` is
   preferred for Entra).

### PASS / FAIL

- **PASS:** step 1 succeeds, step 2 is `AccessDenied`, per provider.
- **FAIL:** step 1 fails for valid users (policy-variable resolution differs
  for that IdP — a fail-closed lockout; revert the parameter to `none` and
  record the provider-specific behavior in ADR-0015's matrix), or step 2
  succeeds (binding is not enforcing — treat as a security finding).

**On pass:** update the per-provider matrix in ADR-0015 and the
`SessionNameBinding` section of COST_ATTRIBUTION.md with dated live results.

---

## LV-11: OIDC `:aud` trust condition — live token exchange per IdP

| | |
|---|---|
| **Claim** | The direct-federation trust policies condition on `${Issuer}:aud = <ClientId>` (`deployment/infrastructure/bedrock-auth-*.yaml`). Cognito was live-verified on 2026-08-14: the correct audience was accepted and a wrong-client token was denied. Other providers remain untested. |
| **Applies to** | Direct STS OIDC mode. |
| **Preconditions** | Auth stack deployed for the provider under test. `SessionNameBinding` can stay `none`. |
| **Cost** | Free. |

### Procedure

Per provider:

1. Positive: `credential-process --profile <profile> --clear-cache` then a
   fresh run — the assume happens under the `aud` condition, so a successful
   credential issuance **is** the live proof for that provider.
2. Negative: obtain an id_token from a *different* app client of the same
   IdP (or decode your token and confirm `aud`), and replay it:

   ```bash
   aws sts assume-role-with-web-identity \
     --role-arn <DirectIAMRole-arn> \
     --role-session-name lv11-negative \
     --web-identity-token "<other-client-id-token>"   # EXPECT: AccessDenied
   ```

### PASS / FAIL

- **PASS:** positive succeeds and negative is denied, per provider.
- **FAIL:** the positive path is denied — that provider's token `aud` does
  not match the pinned client ID; document the divergence in the provider's
  setup guide (`providers/<provider>-setup.md`) before anyone deploys it.

**On pass:** record provider + date in the status table; add a one-line note
to the affected provider setup guide only if behavior diverged.

---

## LV-12: Bearer-token inference through a `bedrock-runtime` VPC endpoint

| | |
|---|---|
| **Claim** | `bedrock:CallWithBearerToken` works through the `com.amazonaws.<region>.bedrock-runtime` interface endpoint. Live-verified in `us-east-1` on 2026-08-14: private DNS resolved through the endpoint, Converse returned `pong`, and CloudTrail recorded `callWithBearerToken=true` with the matching `vpcEndpointId`. Bearer callers have no IAM principal, so the endpoint policy must use `Principal: "*"`. |
| **Applies to** | Deployments using Bedrock API keys (bearer) with PrivateLink. SigV4 through the endpoint is already documented as supported. |
| **Preconditions** | A VPC with a `bedrock-runtime` interface endpoint (private DNS enabled) whose endpoint policy allows `Principal: "*"` for `bedrock:InvokeModel*`; an instance/host inside the VPC with no public egress path to Bedrock; a Bedrock API key. |
| **Cost** | Interface endpoint ≈ $0.01/AZ-hour + one inference call. Delete the endpoint after if created for the test. |

### Procedure

From the in-VPC host:

```bash
# 1. Confirm DNS resolves to the endpoint's private IPs
dig +short bedrock-runtime.<region>.amazonaws.com

# 2. Bearer-token Converse
curl -sS "https://bedrock-runtime.<region>.amazonaws.com/model/us.anthropic.claude-sonnet-4-5-20250929-v1:0/converse" \
  -H "Authorization: Bearer <bedrock-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":[{"text":"Reply with the single word: pong"}]}]}'

# 3. Confirm the call traversed the endpoint: the CloudTrail event for the
#    invocation carries a vpcEndpointId field matching your endpoint
```

### PASS / FAIL

- **PASS:** 200 response and the CloudTrail event shows the `vpcEndpointId`.
  Lift the UNVERIFIED marker.
- **FAIL:** 403 — distinguish the causes: an endpoint policy without
  `Principal: "*"` blocks bearer callers (fix the policy, re-run); a failure
  with a permissive policy means bearer-through-VPCE is not supported —
  change the NETWORK_ISOLATION.md row from "EXPECTED YES" to **NO** with the
  dated evidence. Either outcome closes the item.

**On pass (or fail):** update the bearer row of NETWORK_ISOLATION.md's VPC
endpoint inventory with the observed result and date.

---

## LV-13: Claude Desktop contract ownership

This item is superseded. GIP no longer generates a parallel Claude Desktop
bootstrap contract or deploys its own bootstrap service. Claude Desktop uses
the same Claude Apps Gateway inference/policy plane and either:

- the gateway-native `/user/bootstrap` Desktop overlay; or
- the exact AWS Samples `claude-apps-gateway-bootstrap` companion CDK.

Both upstream subtrees are pinned by repository commit and Git tree ids in
`vendor/aws-samples/anthropic-on-aws/UPSTREAM.json`, then materialized and
checked with versioned `git-tree-sha1-v1` verification by
`scripts/fetch-claude-apps-gateway.sh`, which fails closed on any path, type,
executable-mode, symlink-target, or byte mismatch
([ADR-0036](adr/0036-verified-fetch-claude-apps-gateway.md)).
A scheduled workflow proposes pin bumps, runs upstream tests against the
materialized pin, and opens a review PR. It never auto-deploys or auto-merges changes.

There is therefore no independent GIP response shape to validate against a
real Desktop client. A customer deployment should still smoke-test its own IdP,
MDM, DNS, TLS, and network path using the upstream acceptance steps, but that
is deployment validation rather than a product-contract gap in this repo. No
live Claude Desktop run is claimed by this supersession decision.

---

## LV-14: Collector HTTPS and Claude Desktop internal-ALB restriction

| | |
|---|---|
| **Claim** | Two template-level guarantees of `deployment/infrastructure/otel-collector.yaml` have never been exercised live: (a) `CoWorkServiceToken` is **rejected** when `ALBScheme=internet-facing` (CFN rule), and (b) collector ingress is HTTPS — internet-facing deployments require a certificate and the port-80 listener only redirects to 443. |
| **Applies to** | Central-monitoring deployments. |
| **Preconditions** | Ability to attempt one throwaway monitoring-stack deploy, plus an existing internal-ALB deployment with the shared token for the reachability half. |
| **Cost** | The negative deploy fails before creating billable resources; reachability checks are free. |

Live-verified on 2026-08-14. CloudFormation rejected the
internet-facing/shared-token combination with the expected assertion before
creating resources. A disposable certificate on the internal ALB then proved
HTTP returned 301, an in-VPC shared-token OTLP request returned 200, and the
same endpoint timed out from outside the VPC. The certificate was deleted and
the original development configuration restored.

### Procedure

```bash
# (a) Negative deploy — EXPECT failure at rule evaluation, before resources:
aws cloudformation deploy --region <region> \
  --template-file deployment/infrastructure/otel-collector.yaml \
  --stack-name gip-lv14-negative \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides ALBScheme=internet-facing \
      CoWorkServiceToken=00000000000000000000000000000000 <other-required-params>
# EXPECT: "CoWorkServiceToken is forbidden for internet-facing load
# balancers; use verified OIDC for attributed CoWork telemetry."

# (b) HTTPS behavior on a deployed collector:
curl -sSI "http://<alb-dns-name>/"     # EXPECT: 301/redirect to https://...:443
curl -sS "https://<collector-endpoint>/v1/metrics" \
  -H "X-Cowork-Token: <token>" -H "Content-Type: application/json" -d '{}'
# From INSIDE the network: accepted (2xx). From OUTSIDE: unreachable
# (internal ALB has no public route) — verify both.
```

### PASS / FAIL

- **PASS:** the negative deploy is rejected by the template rule; port 80
  redirects; the shared-token endpoint is reachable only from inside the
  internal network.
- **FAIL:** the internet-facing+token deploy succeeds (the assertion is not
  enforced at deploy time — a real exposure; fix the template), or the token
  path is reachable from outside (network review before relying on the
  aggregate-only shared-token posture in [RUNBOOKS.md §5](RUNBOOKS.md#5-claude-desktop-service-token-rotation)).

### Cleanup

`aws cloudformation delete-stack --stack-name gip-lv14-negative` if any
shell of it exists (a rule failure normally leaves nothing).

**On pass:** note the dated check in MONITORING.md's collector section.

---

## LV-15: Generated harness configs against live CLIs

| | |
|---|---|
| **Claim** | Generated harness configs work against live clients. On 2026-08-14, OpenCode 1.18.18 returned `4` and connected the web-search MCP proxy; Aider 0.86.2 and Pi 0.73.1 worked with Sonnet 4.5; Codex 0.147.0 used governed identity and received the expected Mantle deny. Sonnet 5 payloads were incompatible with Aider/Pi, so generation uses the selected model. |
| **Applies to** | OIDC (the MCP half needs the gateway; the inference half works in any mode). |
| **Preconditions** | Auth stack deployed; `gip package --harnesses all`; installer run on the test machine (it resolves the `__CREDENTIAL_PROCESS_PATH__` placeholder); OpenCode, Codex CLI, and Claude Code installed. Web search stack for the MCP half. Codex only: the default `RestrictToAnthropicModels=true` blocks the OpenAI models Codex requires — expect the documented failure, or redeploy auth with it `false` for the test. |
| **Cost** | One inference call per harness (cents). |

### Procedure

Per harness:

1. **Claude Code (MCP registration):** after install, `claude mcp list`
   shows `agentcore-websearch`; run a prompt that invokes the search tool.
2. **OpenCode:** copy `harnesses/opencode/opencode.json` per
   `harnesses/README.md`; run one prompt — verifies the
   `provider.amazon-bedrock` `options.profile` keys and the CRIS model key.
   Then confirm the `mcp.agentcore-websearch` `type:"local"` shim entry
   lists tools (the shim is
   `credential-process --mcp-proxy <gateway-url>`).
3. **Codex CLI:** apply `harnesses/codex/config.toml`; with
   `RestrictToAnthropicModels=true` expect the documented AccessDenied on
   OpenAI models (that IS the pass for the guardrail); with it `false`, one
   prompt must succeed; confirm the `[mcp_servers.agentcore-websearch]`
   entry lists tools.
4. **Pi / Aider:** inference-only smoke (`AWS_PROFILE=<profile>` + the
   documented flags); no MCP wiring exists by design.

### PASS / FAIL

- **PASS:** each generated config drives a successful model call unmodified,
  and the OpenCode/Codex/Claude Code MCP entries reach the gateway.
- **FAIL:** a config key is rejected by the current CLI version — the vendor
  schema drifted since 2026-07-08; fix the generator
  (`gip package`) and refresh the "Verified against" line in HARNESSES.md.

**On pass:** update the "Verified against" retrieval dates in HARNESSES.md
and note the live execution in ADR-0024.

---

## LV-16: Web Search and Memory outside us-east-1

| | |
|---|---|
| **Claim** | [WEB_SEARCH.md](WEB_SEARCH.md) states the managed Web Search connector is available in **us-east-1 only** "at time of writing" (and the memory stack deploys into the gateway's region). Availability statements go stale in both directions; this item re-tests them. |
| **Applies to** | OIDC. |
| **Preconditions** | None deployed — this is a throwaway stack attempt in a second region. |
| **Cost** | Nothing if creation fails; a gateway has no fixed hourly charge if it succeeds. Delete the stack afterwards either way. |

**2026-08-14 result:** the managed web-search template in `us-west-2` was
rejected by CloudFormation EarlyValidation `PropertyValidation` before any
resources were created. `us-east-1` remains required. Memory was not separately
deployed in `us-west-2`.

### Procedure

```bash
aws cloudformation deploy --region <second-region> \
  --stack-name gip-lv16-websearch \
  --template-file deployment/infrastructure/bedrock-agentcore-gateway.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides DeploymentMode=development AuthType=oidc \
      DiscoveryUrl=<idp-discovery-base-url> ClientId=<client-id>
```

- Success: run one `tools/list` against the new `GatewayMcpEndpoint` (LV-8
  step 2 shape) to confirm the connector actually serves. Then optionally
  deploy the memory stack into the same region and run `gip memory status`.
- Failure: capture the exact error (connector creation vs. resource type
  unavailable) — that is the documentable result.

### PASS / FAIL

Either outcome closes the item; "PASS" = a dated observation recorded:

- Connector works in the second region → update WEB_SEARCH.md's
  prerequisite/residency sections (they currently say us-east-1 only).
- Creation fails → refresh the "at time of writing" date with the tested
  region and error.

### Cleanup

`aws cloudformation delete-stack --stack-name gip-lv16-websearch --region <second-region>`
(and the memory stack first if deployed — its target detaches without
touching the gateway).

**On pass:** update WEB_SEARCH.md (and MEMORY.md's region statements if
memory was tested) with region + date.

---

## LV-17: Validation-fixture sweep

Run **last** — it destroys the fixtures the other items use, and it is the
proof that this runbook left nothing behind.

| | |
|---|---|
| **Claim being validated** | Nothing from this runbook remains billing or polluting the account. |
| **Preconditions** | All other items complete or explicitly deferred with a note in the status table. |
| **Cost** | Read-only assertions ($0); deletions are enumerated first. |

**2026-08-15 result:** the definitive 10:36 UTC sweep found zero validation
resources in both tested regions. Stacks, compute, networking, log groups,
secrets, identity fixtures, guardrails, and inference profiles were all
removed. Account-management-owned resources were excluded from the sweep, and
four customer-managed KMS keys remain in `PendingDeletion` as expected.

**Second-run status (2026-08-15 16:41 UTC):** the pre-floor sweep found no
platform stacks, active instances, VPC endpoints, log groups, secrets, identity
fixtures, guardrails, inference profiles, gateways, memories, or registry
workload identities. One EC2 Mac host and its isolated release
stack/function/role/log remain until 2026-08-16 12:50 UTC. The release uses an
idempotent ten-minute EventBridge schedule plus an independent local watcher;
the cloud function's pre-floor no-op was invoked successfully. Five
customer-managed KMS keys are `PendingDeletion`, and account-management-owned
buckets are excluded. LV-17 returns to **verified** only after the scheduled
release stack self-deletes and the terminal sweep is empty.

### Procedure

```bash
# 1. Everything tagged as a validation fixture
aws resourcegroupstaggingapi get-resources --region <region> \
  --tag-filters Key=gip-validation,Values=live

# 2. Stacks created only for validation (LV-14/LV-16 throwaways, and any
#    platform stack you deployed solely for a test)
aws cloudformation describe-stacks --region <region> \
  --query 'Stacks[?starts_with(StackName, `gip-lv`)].{Name:StackName,Status:StackStatus}'

# 3. Inference profiles (LV-1/LV-3 fixtures)
aws bedrock list-inference-profiles --region <region> --type-equals APPLICATION

# 4. Invocation-logging configuration matches your LV-5 step-0 snapshot
aws bedrock get-model-invocation-logging-configuration --region <region>

# 5. Leftover log groups / alarms created outside stacks
aws logs describe-log-groups --region <region> --log-group-name-prefix <your-test-prefix>
aws cloudwatch describe-alarms --region <region> --alarm-name-prefix gip-lv
```

Every query must return empty (or the pre-validation baseline). Any leftover
is a mutation to remove: enumerate it, delete it, re-run the assertion.
Repeat per region you tested in (LV-16 used a second region).

### PASS / FAIL

- **PASS:** all queries empty/baseline; keep the sweep transcript with your
  validation evidence.
- **FAIL:** any orphan → delete and re-assert. Anything you did not create →
  stop and investigate before touching it.
