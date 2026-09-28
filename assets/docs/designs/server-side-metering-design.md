# Design: Server-Side Tamper-Proof Usage Metering from Bedrock Model Invocation Logs

**Status:** Phase 1 implemented (shadow/reconcile + Phase 2 `max` mode scaffolding; strict deferred) — `deployment/infrastructure/quota-metering.yaml`, `lambda-functions/metering_processor/`, `gip deploy metering`
**Addresses:** `REVIEW.md` finding #3 — quota enforcement trusts client telemetry
**Named as future work in:** `assets/docs/QUOTA_MONITORING.md` ("Future Enhancements")

## 1. Goal & non-goals

**Goal:** produce per-user token usage and estimated cost figures that a developer
cannot suppress or understate from their machine, by metering from Amazon Bedrock
model invocation logging (emitted server-side by the Bedrock service), and feed
those figures into the existing quota enforcement path.

Today usage flows: client OTEL (sidecar or central collector) → CloudWatch metrics →
`quota_monitor` Lambda every 15 min (`deployment/infrastructure/lambda-functions/quota_monitor/index.py:83`,
PromQL `sum_over_time` over `claude_code.token.usage`) → DynamoDB `UserQuotaMetrics`
→ `quota_check` Lambda blocks at credential issuance
(`lambda-functions/quota_check/index.py:48`). In sidecar mode a developer who stops
the sidecar becomes invisible to quotas; detection is detective-only
(`lambda-functions/sidecar_monitor/index.py`, `QUOTA_MONITORING.md` "Sidecar Bypass
Detection").

**Non-goals:**

- **Inline request blocking.** Enforcement remains at credential issuance and
  periodic re-check (`QUOTA_MONITORING.md` "Enforcement Timing"). A blocked user's
  live STS session still works until expiry; that gap is bounded by
  `max_session_duration`, not by this design.
- **Replacing client OTEL.** Dashboards, analytics, model/tool-level breakdowns stay
  on OTEL. This design adds a second, authoritative usage source.
- **Billing-accurate cost.** Estimated cost keeps using `shared/pricing.py` rates;
  AWS CUR remains billing truth (`assets/docs/COST_ATTRIBUTION.md`).
- **Fixing session-name spoofing** (REVIEW.md finding #2). Server-side metering
  reads identity from the assumed-role session name, which is client-asserted in
  Direct STS mode (`source/go/internal/federation/sts.go`) unless the auth stack
  opts into `SessionNameBinding=email|sub` (ADR-0015: `sts:RoleSessionName`
  trust-policy condition bound to the IdP-signed claim). Enabling the binding is
  the prerequisite-for-strict-mode hardening; with it on, this design's identity
  source becomes tamper-proof.

## 2. Source evaluation

| Criterion | Client OTEL (today) | Bedrock model invocation logging | CloudTrail (InvokeModel et al.) |
|---|---|---|---|
| Identity fidelity | `user.email` OTEL dimension, set by client — trivially suppressed/forged | `identity.arn` field, "captured automatically" by the service; session name = email in Direct STS/IDC modes; **not** email in Cognito federation mode (see §3) [1] | `userIdentity.arn`, service-captured; same session-name caveat [2] |
| Token counts | input/output/cacheRead/cacheCreation per model (full fidelity) | `input.inputTokenCount`, `output.outputTokenCount` top-level; cache read/write counts only inside the response body (`usage` block) — see §6 privacy tension and Open Q1 [1][6] | **None.** Example `InvokeModel` entry shows `responseElements: null`; only `requestParameters.modelId` [2] |
| Model id | OTEL `model` dimension | `modelId` = "model ID or inference profile ID used for the invocation" [1] | `requestParameters.modelId`; CRIS destination in `additionalEventData.inferenceRegion` [3] |
| Latency | 15-min metering cadence (`quota-monitoring.yaml:280`) | Near-real-time delivery to CW Logs; delivery health observable via `ModelInvocationLogsCloudWatchDeliverySuccess/Failure` metrics [4] | Management events, typically minutes; `LookupEvents` API is rate-limited (used by `sidecar_monitor`) |
| Cost at 1k/10k users | Already deployed (collector/sidecar + CW metrics) | ≈$40 / ≈$400 per month, metadata-only (see §6) | $0 extra for management events, but unusable for metering (no token counts) |
| Tamper resistance | **None** — stop sidecar, edit exporter | **High** — emitted by the Bedrock service account-side; user IAM policy grants only `bedrock:InvokeModel*` (`deployment/infrastructure/bedrock-auth-okta.yaml:100-124`), no `bedrock:*LoggingConfiguration` | **High** — same trust level |

**Decision: Bedrock model invocation logging is the sole accrual source.**
CloudTrail lacks token counts, so it cannot meter; it stays in its current
detective role (`sidecar_monitor`) and gains a coverage-check role (§7.5). Using
exactly one accrual source also resolves dedup by construction (§7.2).

Citations:
[1] https://docs.aws.amazon.com/bedrock/latest/userguide/model-invocation-logging.html (log entry format: `identity.arn`, `inputTokenCount`, `outputTokenCount`, `modelId`, `region`, `requestId`, `operation`; includes a Logs Insights query grouping token usage by `identity.arn`)
[2] https://docs.aws.amazon.com/bedrock/latest/userguide/logging-using-cloudtrail.html (`InvokeModel`, `InvokeModelWithResponseStream`, `Converse`, `ConverseStream` logged as management events; example entry with `responseElements: null`)
[3] https://docs.aws.amazon.com/bedrock/latest/userguide/cross-region-inference.html ("All cross-Region inference requests are logged in CloudTrail in your source Region … `additionalEventData.inferenceRegion`")
[4] https://docs.aws.amazon.com/bedrock/latest/userguide/monitoring-runtime-metrics.html (invocation-log delivery success/failure metrics under `AWS/Bedrock`)
[5] https://docs.aws.amazon.com/bedrock/latest/APIReference/API_LoggingConfig.html (`textDataDeliveryEnabled`, `imageDataDeliveryEnabled`, `embeddingDataDeliveryEnabled`, `videoDataDeliveryEnabled`, `audioDataDeliveryEnabled` booleans; CW Logs and/or S3 destinations)
[6] https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html ("total input tokens = inputTokens + cacheReadInputTokens + cacheWriteInputTokens" — i.e. `inputTokens` excludes cache tokens)

## 3. Chosen architecture

Per allowed Bedrock region: invocation logging (metadata-only) → CloudWatch Logs →
subscription filter → regional processor Lambda → cross-region writes into the
existing `UserQuotaMetrics` table in the quota region. No schema migration —
server-side figures are **additive attributes** on the same item.

```
 region A (e.g. us-east-1)                       region B ... N (each region in
┌──────────────────────────────────────┐          AllowedBedrockRegions)
│ Bedrock model invocation logging     │         ┌──────────────────────────┐
│  (account/region singleton config,   │         │ same per-region trio:    │
│   text/image/video/audio/embedding   │         │  logging cfg (custom     │
│   data delivery = OFF)               │         │  resource) + log group   │
│        │                             │         │  + filter + processor    │
│        ▼                             │         └────────────┬─────────────┘
│ CW Logs group                        │                      │
│  /aws/bedrock/modelinvocations       │                      │
│  (retention 30d)                     │                      │
│        │ subscription filter         │                      │
│        ▼                             │                      │
│ metering_processor Lambda            │                      │
│  1. parse log events (JSON)          │                      │
│  2. identity.arn → email             │                      │
│     (assumed-role/<Role>/<session>,  │                      │
│      same parse as sidecar_monitor   │                      │
│      _extract_email_from_arn and     │                      │
│      quota_check IDC path)           │                      │
│  3. modelId → family → $ via         │                      │
│     shared/pricing.py                │                      │
│  4. batch-aggregate per (email)      │                      │
│  5. dedup on requestId               │                      │
└────────┼─────────────────────────────┘                      │
         │  UpdateItem ADD server_* (cross-region API call)   │
         ▼                                                    ▼
        ┌───────────────────────────────────────────────────────┐
quota   │ DynamoDB UserQuotaMetrics (existing, quota region)    │
region  │  pk=USER#email sk=MONTH#yyyy-mm                       │
        │  existing: total_tokens, daily_tokens, estimated_cost,│
        │            daily_cost_usd, input/output/cache_tokens  │
        │  NEW (additive): server_total_tokens,                 │
        │   server_input_tokens, server_output_tokens,          │
        │   server_estimated_cost, server_daily_tokens,         │
        │   server_daily_cost_usd, server_daily_date,           │
        │   server_last_updated, server_regions (string set)    │
        └──────────────┬────────────────────────────────────────┘
                       │ GetItem (unchanged access pattern)
                       ▼
        quota_check Lambda: effective_usage = max(client, server)
        quota_monitor Lambda: + reconciliation/drift (see §4)
```

Key points:

- **Identity mapping.** `identity.arn` ends in the STS role session name. Direct
  STS: session name = sanitized email (`sts.go:86-103`; sanitizer preserves
  `[\w+=,.@-]`, so emails survive intact). IDC: session name = email or IDC
  username — reuse the exact parse in `quota_check/index.py:72-88`. **Cognito
  federation mode** (`bedrock-auth-*.yaml` `FederationType=cognito`): the session
  name is set by Cognito, not the email, so `identity.arn` alone cannot be mapped;
  events aggregate under `USER#UNATTRIBUTED#<role>` and raise an alarm (Open Q3).
- **Same schema, additive attrs.** `server_*` attributes ride on the existing
  `pk=USER#{email}` / `sk=MONTH#{YYYY-MM}` item (`quota-monitoring.yaml:152-174`,
  schema documented in `QUOTA_MONITORING.md` "Data Schema"). `UpdateExpression ADD`
  mirrors `quota_monitor.update_quota_metrics` (`quota_monitor/index.py:225-248`),
  including the `server_daily_date` reset guard.
- **Enforcement on max(client, server).** `quota_check.get_user_usage`
  (`quota_check/index.py:443`) returns
  `total = max(total_tokens, server_total_tokens)` (same for daily and cost
  fields). Absent `server_*` attributes read as 0, so behavior is byte-identical
  for deployments without the metering stack — no migration, no backfill.
- **Cost calculation** reuses `lambda-functions/shared/pricing.py`
  (`resolve_model_family`, `get_rates`) against the log's `modelId` — the same
  rates as the client path, so drift (§4) measures data loss, not rate skew.
- **Why not Firehose?** Metadata-only entries are ~1–2 KB and the consumer is a
  DynamoDB upsert, not S3 analytics. Subscription filter → Lambda is the smallest
  design that meets the 15-min cadence, avoids a new S3/Glue surface, and matches
  the repo's existing Lambda patterns. At 10k+ users, switch the subscription
  target to Firehose → S3 + batch aggregation (noted in §6/§7.3), without changing
  the DDB contract.

## 4. Reconciliation (client vs server)

Extend `quota_monitor` (already scheduled every 15 min,
`quota-monitoring.yaml:275-284`) with a reconciliation step after its existing
threshold pass:

1. For each `USER#…/MONTH#…` item already scanned
   (`quota_monitor/index.py:299-316`), compute
   `drift = (server_x - client_x) / max(server_x, 1)` for
   `x ∈ {input_tokens+output_tokens, estimated_cost}` on **month-to-date
   cumulative** values (cumulative smooths window-alignment noise between the
   15-min PromQL cadence and near-real-time log delivery). Compare
   input+output only — not cache tokens — until Open Q1 is resolved.
2. Publish CloudWatch metrics, namespace `GIP/Metering`:
   `MeteringDriftPercent` (per `user.email`), `MeteringDriftUserCount`
   (users above threshold), `UnattributedTokens`.
3. CloudWatch alarm (in the new stack) on `MeteringDriftUserCount > 0` for 2
   consecutive periods, notifying the existing `QuotaAlertTopic`
   (`quota-monitoring.yaml:177-186`). Default threshold `DriftAlertPercent=25`
   (parameter): large enough to absorb OTEL export timing and token-accounting
   differences, small enough that a stopped sidecar (client→0, drift→100%) fires
   within ~30 min. This catches **both** sidecar stoppage and OTEL pipeline bugs
   (e.g. the delta-temporality regression documented at
   `quota_monitor/index.py:83-99`).
4. **Strict mode** (Phase 3): `quota_check` env `METERING_MODE=strict` makes
   enforcement read `server_*` figures only, ignoring client figures. Modes:
   `shadow` (default; collect + reconcile, enforce on client as today), `max`
   (enforce on max), `strict`.

Once drift alarms are quiet, `EnableBypassDetection`
(`quota-monitoring.yaml:53-64`) becomes redundant and can be retired — the drift
metric subsumes it with better precision.

## 5. Region fan-in

Invocation logging is a **per-account, per-region singleton** configuration, and
its S3/CW destinations must be in the same region [1][5]. Two facts bound the
problem:

- Users can invoke Bedrock only in `AllowedBedrockRegions`
  (`bedrock-auth-okta.yaml:112-114` `aws:RequestedRegion` condition; siblings
  identical).
- With CRIS, logs stay in the **source** region: "customer-managed logs (such as
  model invocation logging) … remain exclusively within the source Region"
  (https://aws.amazon.com/blogs/machine-learning/securing-amazon-bedrock-cross-region-inference-geographic-and-global/,
  2026-01) and CloudTrail records CRIS in the source region [3]. **CRIS
  destination regions are irrelevant to collection.** Only source regions matter.

So: deploy the per-region trio (logging config + log group + filter + processor)
to every region in `AllowedBedrockRegions`, which per profile
(`source/governed_inference_platform/models.py`, sonnet-4-6 entry at
`models.py:110-197`; other models equivalent) means:

| Profile | Source regions to instrument | Count |
|---|---|---|
| `us` | us-east-1, us-east-2, us-west-1, us-west-2, ca-central-1, ca-west-1 | 6 |
| `eu` | eu-central-1, eu-north-1, eu-south-1, eu-south-2, eu-west-1, eu-west-3 | 6 |
| `jp` / `au` | ap-northeast-1, ap-northeast-3 / ap-southeast-2, ap-southeast-4 | 2 each |
| `global` | all ~32 commercial source regions listed at `models.py:136-169` | ~32 |

Fan-in mechanism: the regional processor Lambda calls the DynamoDB **regional
endpoint of the quota region** directly (plain cross-region API call; no Global
Tables, no cross-region subscription plumbing). `gip deploy metering` iterates
`AllowedBedrockRegions` and deploys the same template N times with a
`QuotaRegion`/`QuotaTableName` parameter — same pattern the CLI already uses for
multi-region concerns in `deploy.py`.

`global`-profile note: ~32 stacks is heavy. Practical mitigation, documented in
the stack README: narrow `AllowedBedrockRegions` to the regions your developers
actually sit near (the IAM condition already enforces it); §7.5's coverage check
alarms if anyone invokes from an uninstrumented region.

**EU residency:** log groups and processing stay in-region per source region; only
the aggregated counters (email, token integers, USD) cross into the quota region —
consistent with the existing posture where `UserQuotaMetrics` already holds emails
in the quota region (REVIEW.md finding #16 applies unchanged).

## 6. Privacy & cost controls

**Prompt capture OFF.** The logging config exposes independent booleans —
`textDataDeliveryEnabled`, `imageDataDeliveryEnabled`,
`embeddingDataDeliveryEnabled`, `videoDataDeliveryEnabled`,
`audioDataDeliveryEnabled` [5]. The custom resource sets **all five to `false`**,
so no prompt/completion bodies are delivered; the entry retains metadata:
`identity.arn`, `modelId`, `operation`, `region`, `requestId`, token-count fields
[1]. This is non-negotiable in this design: the quota system must never become a
prompt-content store. Tension to verify (Open Q1): cache read/write token counts
live in the response body's `usage` block [6], which body-off delivery may omit —
Phase 1 empirically confirms which count fields survive metadata-only mode.
No `largeDataDeliveryS3Config` is configured (nothing large to deliver).

**Retention:** CW log group retention 30 days (parameter, default 30) — the data
is consumed within seconds and only needed for replay/audit. No S3 destination by
default (one less data store holding `identity.arn`).

**Estimated monthly cost** (assumptions: [1,000] invocations/developer/working
day — placeholder to validate against a real deployment's `Invocations` metric —
22 working days, ~1.5 KB/entry metadata-only, CW Logs Standard ingestion
$0.50/GB (https://aws.amazon.com/cloudwatch/pricing/), DynamoDB on-demand
$0.625/M WRU (https://aws.amazon.com/dynamodb/pricing/on-demand/), Lambda 256 MB):

| Developers | Events/mo | CW ingest | DDB (dedup + aggregated writes) | Lambda | Total ≈ |
|---|---|---|---|---|---|
| 100 | 2.2M | ~3.3 GB → $1.7 | ~$1.8 | <$1 | **~$5** |
| 1,000 | 22M | ~33 GB → $17 | ~$18 | ~$2 | **~$40** |
| 10,000 | 220M | ~330 GB → $165 | ~$180 | ~$20 | **~$400** |

Costs scale linearly with invocation volume. The DDB column doubles under the
transactional dedup+accrual writes (§7.3: 2× WCU → ~$3.6 / ~$36 / ~$360) —
still noise below ~1k developers. At the 10k tier, moving the
subscription target to Firehose→S3 (Firehose $0.029/GB,
https://aws.amazon.com/firehose/pricing/) with 5-min batch aggregation cuts the
DDB dedup line by ~10× and is the documented scale-up path. Compare: quota stack
today is "$2–10/mo for <1k users" (`QUOTA_MONITORING.md` "Cost Considerations").

## 7. Failure modes

1. **Log delivery lag vs 15-min cadence.** CW Logs delivery is near-real-time;
   even multi-minute lag is strictly better than today's 15-min PromQL window.
   Delivery failures are visible via `ModelInvocationLogsCloudWatchDeliveryFailure`
   [4] — alarm on it in the new stack. Enforcement latency is unchanged (bounded
   by credential issuance/re-check, `QUOTA_MONITORING.md` "Enforcement Timing").
2. **Dedup across sources.** Only invocation logs accrue `server_*` tokens.
   CloudTrail is never accrued (it has no counts [2]), so the same request can
   never be counted twice across sources by construction. `sidecar_monitor` keeps
   its CloudTrail detective role until retired (§4).
3. **Processor retries / throttling.** Subscription-filter → Lambda is async;
   retries redeliver the same batch. `ADD` is not idempotent, so the processor
   dedupes on `pk=DEDUP#<requestId>` markers (conditional Put, TTL 24 h) —
   and commits the markers **atomically with the accrual**: per user, chunked
   `TransactWriteItems` of ≤99 conditional marker Puts + 1 accrual `Update`
   (≤100 actions/transaction). Already-accrued requestIds surface as
   `ConditionalCheckFailed` cancellation reasons, are dropped from the chunk,
   and the remainder retries; any other cancellation commits nothing and
   fails the invocation. A partial-batch failure therefore leaves no markers
   behind, so retries and DLQ redrives are genuinely idempotent — no
   under-count, no double count. (The original design wrote each marker
   before the deferred accrual loop; a failure between the two made the
   retry read the whole batch as duplicates, permanently under-counting —
   Wave-3 review F4/R5. Transactional writes cost 2× WCU on this path:
   ~$3.6/mo at 100 developers, ~$36/mo at 1k — see §6.) Reserved concurrency
   (default 10/region) + SQS DLQ for failed batches. Within a batch, events
   are pre-aggregated per email (one accrual `Update` per user per chunk),
   cutting write volume ~10× for agentic bursts.
4. **DDB hot key at month rollover.** Partition key is per-user (`USER#{email}`),
   so writes are spread; the rollover moment only changes `sk` and creates fresh
   items — no shared partition is written on this path (the shared `ALERTS`
   partition belongs to `quota_monitor`). A single user's write rate is bounded by
   their own invocation rate (≪ 1,000 WCU/item). Daily reset uses the same
   `server_daily_date` guard as `quota_monitor/index.py:237-241` to avoid
   cross-day races.
5. **Coverage gap (uninstrumented region).** If logging config is deleted or a
   region was never instrumented, usage there is silently unmetered. Mitigation:
   reconciliation step compares regions seen in CloudTrail management events
   (reusing `sidecar_monitor`'s lookup) against `server_regions`; mismatch →
   `MeteringCoverageGap` metric + SNS alert. Also alarm if
   `GetModelInvocationLoggingConfiguration` drifts from expected (weekly check).
6. **Existing customer logging config.** The per-region config is a singleton;
   `PutModelInvocationLoggingConfiguration` would clobber a customer's existing
   setup. The custom resource **fails closed** if a config exists, unless
   `AdoptExistingConfig=true` (then it only attaches a subscription filter to the
   customer's existing log group and touches nothing else). Never silently
   overwrite; on stack delete, delete the config only if this stack created it.

## 8. Delivery plan

**Recommendation: new optional stack `deployment/infrastructure/quota-metering.yaml`**
(not an extension of `quota-monitoring.yaml`), because it (a) deploys N times —
once per allowed Bedrock region — while quota-monitoring is single-region, (b)
owns an account-level singleton (logging config) with adopt/fail semantics that
must be independently creatable/destroyable, and (c) stays default-OFF without
touching the existing stack's update path. It imports
`${QuotaStackName}-QuotaTableName`/`-QuotaTableArn` exports
(`quota-monitoring.yaml:543-554`) in the quota region and takes
`QuotaRegion` as a parameter elsewhere.

CFN contents (per `deployment/CLAUDE.md` rules — partition-aware ARNs, positive
conditions, exact IAM actions):

- Custom resource Lambda (`metering_config`): `bedrock:PutModelInvocationLoggingConfiguration`,
  `Get…`, `Delete…`; all data-delivery booleans false; adopt/fail logic (§7.6).
- `AWS::Logs::LogGroup` (+retention), Bedrock logging IAM role
  (`bedrock.amazonaws.com` trust with `aws:SourceAccount`/`aws:SourceArn`
  conditions per [1]), `AWS::Logs::SubscriptionFilter` → processor.
- `metering_processor` Lambda (new dir
  `lambda-functions/metering_processor/`, bundling `shared/pricing.py` the same
  way `quota_monitor` does): parse, map identity, price, dedup, aggregate,
  cross-region `UpdateItem` scoped to the two table ARNs. SQS DLQ.
- Drift/coverage alarms + reuse of `QuotaAlertTopic` (imported).

Small diffs to existing components (all backwards-compatible defaults):

- `quota_check/index.py`: `max(client, server)` in `get_user_usage` +
  `METERING_MODE` env (default `shadow` → behavior unchanged).
- `quota_monitor/index.py`: reconciliation step + metrics (no-op when no
  `server_*` attrs exist).
- CLI: `metering` added to `VALID_STACKS` (`cli/commands/deploy.py:36`) and
  `DESTROYABLE_STACKS` (`cli/commands/destroy.py:21`, destroy before `quota`);
  `gip init` prompt gated on quota monitoring being enabled, **default No**.
- Docs: section in `QUOTA_MONITORING.md`; remove the corresponding Future
  Enhancements bullet when shipped.

**Backwards compatibility:** stack absent → no `server_*` attributes → `max()`
degenerates to client figures; existing deployments see zero behavior change.
New table attributes are additive on existing items (same non-breaking `ADD`
pattern as `cost_usd`, `QUOTA_MONITORING.md` "Backward compatible").

**Test plan:**

- Unit: ARN→email parser (Direct STS sanitized emails, IDC `AWSReservedSSO` with
  and without `@` — mirror `quota_check/index.py:72-88` cases; Cognito ARN →
  unattributed), pricing parity with `shared/pricing.py`, dedup idempotency,
  daily-reset guard, `max()` fallback when `server_*` absent (regression per
  `source/CLAUDE.md`: fails without fix).
- Integration (test account): enable stack in 2 regions; drive traffic via
  `gip test`; assert `server_*` accrual matches the Bedrock response `usage`
  within 1%; **kill the sidecar**, keep invoking, assert drift alarm fires and
  Phase-2 `max()` blocks at the limit; verify which token-count fields survive
  metadata-only logging (Open Q1) and record findings in the doc.
- `cfn-lint deployment/infrastructure/quota-metering.yaml`; stack
  create→update→delete cycles including `AdoptExistingConfig` and
  delete-with-preexisting-config.

**Phased rollout:**

- **Phase 1 — shadow/reconcile-only (default when enabled):** collect `server_*`,
  publish drift, alarm; enforcement still client-only. Run ≥2 weeks; validates
  identity mapping, cache-token question, cost model.
- **Phase 2 — enforce-on-max:** `METERING_MODE=max`. A stopped sidecar no longer
  reduces enforced usage (server figure keeps counting). Client figure still
  protects against server-side collection gaps.
- **Phase 3 — strict:** `METERING_MODE=strict`; server figures are the sole
  enforcement input. Prerequisites: Open Q1 resolved (cache tokens counted or
  consciously waived), session-name binding landed (REVIEW.md #2), coverage
  alarms quiet for a full billing cycle.

## 9. Open questions for maintainers

1. **Cache token fidelity (blocking for strict mode):** does the invocation log's
   `input.inputTokenCount` include cache read/write tokens, or do they only
   appear in the response body's `usage` block ([6] implies `inputTokens`
   excludes them)? And are token-count fields still populated when
   `textDataDeliveryEnabled=false`? Cache-write is typically the majority of
   Claude Code tokens (`quota_monitor/index.py:70-74`), so under-counting it makes
   server figures structurally low vs client figures. Needs an empirical check in
   Phase 1; fallback is enforcing on input+output only with a documented caveat.
2. **Singleton config policy:** is fail-closed-with-`AdoptExistingConfig` the
   right stance for customers who already run invocation logging (e.g. into a
   SIEM), or should we support attaching only a subscription filter to an
   externally-managed log group as a first-class mode?
3. **Cognito federation mode:** session name is not the user's email there, so
   invocation-log identity cannot be mapped per-user. Do we (a) document
   server-side metering as Direct STS/IDC-only, (b) require migration to Direct
   STS to enable Phase 2+, or (c) invest in a CloudTrail `requestId` join to
   recover identity for Cognito sessions?
4. **Region scope for `global` profile:** instrument all ~32 source regions, or
   require admins to narrow `AllowedBedrockRegions` when enabling metering (with
   the coverage-gap alarm as the safety net)?
5. **Bypass-detection retirement:** once drift alarms are proven, do we deprecate
   `EnableBypassDetection`/`sidecar_monitor` in the same release or keep both for
   one release cycle?
