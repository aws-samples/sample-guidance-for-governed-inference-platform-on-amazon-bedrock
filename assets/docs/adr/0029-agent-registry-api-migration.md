# 0029 — Agent Registry preview→GA API migration (dual pinned surfaces, env cutover)

Status: Accepted · 2026-07-29 · budgeted migration task from ADR-0017
Supports: ADR-0017 (skills registry on Agent Registry)

## Context

ADR-0017 accepted the risk that the Agent Registry preview API breaks:
AWS moves the service from the `bedrock-agentcore` namespace to a dedicated
`agent-registry` namespace on **2026-08-06** (GA), and shuts down the old
preview endpoints on **2026-09-17**. AWS published the full migration guide
before GA, so the new surface is documented — but the new namespace does not
exist until 2026-08-06, current botocore has no `agent-registry-control`
service model, and registry data does NOT carry over automatically.

Source for every API fact below: "Comprehensive registry migration guide"
(https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-faq.html,
retrieved 2026-07-29), referred to as *registry-faq* here.

## Decision

Both pinned client modules (`skills_registry/registry_client.py`,
`cli/utils/agent_registry.py`) now carry **two pinned, dated API surfaces**
and a single cutover switch:

- `preview-2026-07-08` → `boto3.client("bedrock-agentcore-control")` — the
  behavior shipped today, unchanged. Default.
- `ga-2026-08-06` → `boto3.client("agent-registry-control")` — implements the
  documented GA schema.
- Switch point: the `GIP_AGENT_REGISTRY_API_SURFACE` environment variable
  (one knob for CLI and Lambda; a per-instance `surface=` override exists for
  tests). Unknown values fail with a `ValueError` naming this runbook.

GA-surface changes implemented (registry-faq changes 1–3, 6):

| Preview | GA |
| --- | --- |
| `descriptorType="AGENT_SKILLS"` | top-level `recordType="SKILL"` |
| record `name` | `displayName`; new required dedup `name` (unique with `recordVersion`) — we send both = skill name |
| `descriptors.agentSkills.skillDefinition.{inlineContent, schemaVersion}` | `descriptors.agentSkillsDefinition.{data, dataSchemaVersion}` |
| `descriptors.agentSkills.skillMd.inlineContent` | `agentSkillsDefinition.additionalData.skillMd.data` |
| List filters as discrete params (`status=`, `descriptorType=`) | one `filters=[{"name","values"}]` param; we filter on `recordType`/`status` |
| CreateRegistry `authorizerType=` | wrapped as `discoveryConfiguration={"authorizerType": ...}` |

Record readers (`skills_cmd.extract_record_meta`,
`distributor._record_definition`) accept **both** descriptor shapes, so
reading works before, during, and after cutover.

Fail-visible break detection (preview surface, real-client construction only):

- from 2026-08-06: prints a `WARNING:` naming the shutdown date, the env
  switch, and this runbook;
- from 2026-09-17: raises `RuntimeError` (the endpoints are dead anyway; a
  named error beats an opaque 4xx). Explicitly setting
  `GIP_AGENT_REGISTRY_API_SURFACE=preview-2026-07-08` is the escape hatch if
  AWS extends the deadline.
- Selecting the GA surface on a botocore without the `agent-registry-control`
  model raises a `RuntimeError` telling the operator to upgrade boto3.

## Known / Unknown

**Known (documented, implemented, unit-tested against the documented shapes):**
namespace + endpoints (`agent-registry-control.{region}.api.aws`), SDK client
names, unchanged operation names, record/registry schema changes above,
structured list filters, IAM action prefix `agent-registry:*`, new ARN
namespace, EventBridge source `aws.agent-registry`, CloudTrail source
`agent-registry.amazonaws.com`, migration tooling in agentcore-samples from
2026-08-06.

**Unknown until GA ships (deliberately NOT guessed):**

1. **`UpdateRegistryRecord` GA request shape.** The preview PATCH uses nested
   `optionalValue` wrappers; registry-faq documents the resource model but
   not the GA update-request wrappers. `update_skill_definition` **raises**
   on the GA surface with a message naming this file. The distributor's
   approve path calls it only when promotion changes the artifact block —
   i.e. every first approval — so approvals are blocked on the GA surface
   until this is verified. Day-one step 5 below.
2. **GA response key shapes** for Create/List (e.g. `recordId` vs `recordArn`
   vs wrapped `record`). Clients already tolerate all three and raise loudly
   otherwise.
3. Exact boto3/botocore minimum version carrying the GA service model.
4. Whether `UpdateRegistry`'s GA description parameter keeps its preview
   shape (we pass it through unchanged).

**Out of code scope here (template/IAM), required day-one:**
`deployment/infrastructure/skills-registry.yaml` pins `bedrock-agentcore:*`
IAM actions, `arn:*:bedrock-agentcore:*` resource ARNs, and the
`aws.bedrock-agentcore` EventBridge source, and does not yet set
`GIP_AGENT_REGISTRY_API_SURFACE` in the Lambda `Environment` blocks.

## Registry state replay (verifying ADR-0017's re-creatability claim)

Verified against the code: the registry owns only approval state.

- Skill sources are directories in git; artifacts live in S3 under
  `skills/<name>/<version>/` (review sources + canonical zip) and
  `approved/sha256/<digest>/` (promoted, immutable) — the bucket is untouched
  by the namespace migration.
- `gip skills publish <dir>` is replay-idempotent: the zip build is
  deterministic (fixed timestamps, sorted entries), and the S3 reservation
  path (`IfNoneMatch:"*"` + byte-compare on 412) verifies rather than
  duplicates existing identical objects, then registers a fresh record.
- If a published version's source dir is no longer in the working tree at
  that exact version, reconstruct it from
  `aws s3 cp --recursive s3://<artifact-bucket>/skills/<name>/<version>/ ./<name>/`.

Replay commands, per active skill:

```
gip skills publish <skill-dir>            # re-create + submit the record
gip skills approve <name>@<version>       # curator re-approves; distributor re-renders
gip skills list --status APPROVED         # verify
gip skills sync                           # consumer verification
```

Alternative to replay: the AWS migration script (agentcore-samples, available
2026-08-06) copies records preview→GA wholesale. Replay is preferred for our
scale (regenerates records through our own validation path); the script is
the fallback for many-record registries.

## Day-one runbook (2026-08-06)

1. Upgrade boto3/botocore to the GA release in the CLI env and the Lambda
   bundles (`skills_registry` provisioner + distributor).
2. Update `deployment/infrastructure/skills-registry.yaml`: IAM action prefix
   `bedrock-agentcore:` → `agent-registry:`, resource ARNs
   `arn:*:bedrock-agentcore:` → `arn:*:agent-registry:`, EventBridge source
   `aws.bedrock-agentcore` → `aws.agent-registry`, and add
   `GIP_AGENT_REGISTRY_API_SURFACE: ga-2026-08-06` to both Lambda
   `Environment.Variables` blocks.
3. Fetch the GA `UpdateRegistryRecord` API reference; implement the GA branch
   of `update_skill_definition` in BOTH client modules (currently
   fail-loud); add the shape test next to
   `test_registry_client_patches_only_agent_skill_definition`.
4. `gip deploy skills` — the `Custom::AgentRegistry` provisioner adopts or
   creates the registry in the NEW namespace (new `RegistryId` output;
   profile fields refresh from stack outputs).
5. Replay approved skills (commands above) or run the AWS migration script.
6. Set `GIP_AGENT_REGISTRY_API_SURFACE=ga-2026-08-06` for CLI users
   (managed settings / shell profile).
7. Verify: `gip skills list`, one end-to-end publish→approve→sync, distributor
   scheduled render writes `marketplace.json` + `skills-lock.json`.
8. Before 2026-09-17: optionally delete the old preview registry (it is
   retained by `RetainOnDelete`); after that date it is unreachable anyway.

## Day-one execution (2026-08-12)

Executed six days late (the runbook was not run on 2026-08-06). GA shapes
were verified against the installed botocore service model — NOT guessed:
botocore **1.43.69** ships `agent-registry-control/2025-12-01`
(`serviceId: Agent Registry Control`, protocol rest-json, signing name
`agent-registry`). The local dev venv (botocore 1.43.46) does NOT have the
model yet; a throwaway venv with latest boto3/botocore was used to inspect
it.

**Resolved unknowns (from the model, inspected 2026-08-12):**

1. **`UpdateRegistryRecord` GA request shape (unknown 1) — VERIFIED.** GA
   keeps PATCH-style `optionalValue` wrappers at every updatable level:
   `descriptors.optionalValue.agentSkillsDefinition.optionalValue.{data.optionalValue,
   dataSchemaVersion.optionalValue, additionalData.optionalValue.skillMd.optionalValue.…}`.
   `update_skill_definition` is now implemented on the GA surface in BOTH
   client modules (the CLI twin gained the method for parity); the
   fail-loud `RuntimeError` is gone. Shape tests sit next to
   `test_registry_client_patches_only_agent_skill_definition`.
2. **GA response keys (unknown 2):** `CreateRegistryRecord` returns only
   `{recordArn, status}` — the existing ARN-parsing fallback covers it. GA
   record ARNs keep the `registry/<id>/record/<id>` path (new namespace
   `arn:*:agent-registry:*`). `ListRegistryRecords` returns
   `registryRecords` summaries including `recordId`/`status`; filter names
   are model-pinned to `name|status|recordType` (matches our filters).
3. **Minimum SDK (unknown 3):** botocore 1.43.69 carries the model
   (1.43.46 does not; exact first-shipping release not bisected).
4. **`UpdateRegistry` description (unknown 4) — the preview shape did NOT
   carry over:** GA wraps it as `description={"optionalValue": ...}`.
   Implemented in the Lambda client's GA branch.

**Changes landed:**

- Default surface flipped to `ga-2026-08-06` in both client modules
  (`GIP_AGENT_REGISTRY_API_SURFACE=preview-2026-07-08` is the explicit
  fallback; the botocore-too-old error now names that exact setting).
- `skills-registry.yaml`: new `AgentRegistryApiSurface` parameter (default
  `ga-2026-08-06`) wired as `GIP_AGENT_REGISTRY_API_SURFACE` into BOTH
  Lambda `Environment` blocks. Deviation from step 2 above: instead of a
  hard `bedrock-agentcore:` → `agent-registry:` cutover, IAM actions/ARNs
  are **dual-granted** and the EventBridge rule matches **both** sources
  (`aws.bedrock-agentcore`, `aws.agent-registry`) — the parameter can
  legitimately select either surface until the preview endpoints die on
  2026-09-17, and registry data migration (replay) may not have happened
  yet in a given account. Remove the `bedrock-agentcore` grants and event
  source after 2026-09-17. `RegistryArn` output follows the selected
  surface via the `UsesGaSurface` condition.
- The CLI dependency floor is boto3/botocore 1.43.69. `gip deploy skills`
  and `scripts/publish-templates.sh` stage the Lambda source with boto3 and
  botocore 1.43.70 bundled, so the GA service model does not depend on the
  Lambda runtime's SDK version.

**Remaining (live verification — steps 1, 4–8 above still to run):**

- `gip deploy skills` (provisioner creates/adopts the GA-namespace
  registry), replay approved skills, then the end-to-end
  publish→approve→sync check — the GA `UpdateRegistryRecord` shape is
  verified against the SDK model but has not yet been exercised against
  the live service.
- Confirm the GA EventBridge event uses source `aws.agent-registry` with
  the same detail-type string.
