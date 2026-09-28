# ABOUTME: CLI-side isolation point for every AWS Agent Registry API call (both API surfaces)
# ABOUTME: Mirrors deployment/infrastructure/lambda-functions/skills_registry/registry_client.py

"""Agent Registry client for the ``gip skills`` commands.

TWO PINNED API SURFACES, selected by one switch point (the
``GIP_AGENT_REGISTRY_API_SURFACE`` environment variable, default GA since
the 2026-08-12 day-one execution of ADR-0029):

``preview-2026-07-08`` — boto3 client("bedrock-agentcore-control"):
    create_registry, get_registry, update_registry, delete_registry,
    list_registries, create_registry_record, get_registry_record,
    update_registry_record, delete_registry_record, list_registry_records,
    submit_registry_record_for_approval, update_registry_record_status

``ga-2026-08-06`` — boto3 client("agent-registry-control")  (DEFAULT).
  Launched 2026-08-06 (preview endpoints shut down 2026-09-17); requires a
  botocore release shipping the ``agent-registry-control`` service model
  (verified: botocore 1.43.69, model agent-registry-control/2025-12-01).
  Operation names are unchanged; record schema changes: descriptorType ->
  recordType="SKILL", name -> displayName plus a new required dedup name,
  descriptors.agentSkills -> descriptors.agentSkillsDefinition {data,
  dataSchemaVersion, additionalData.skillMd.data}, list filters ->
  structured ``filters`` param. UpdateRegistryRecord keeps PATCH-style
  ``optionalValue`` wrappers at every level (verified 2026-08-12 against
  the botocore service model, ADR-0029 day-one execution).
  Source: "Comprehensive registry migration guide" (retrieved 2026-07-29):
  https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-faq.html

The Lambda runtime has its own copy in
lambda-functions/skills_registry/registry_client.py — keep the two in sync
when migrating. Cutover procedure and day-one runbook:
assets/docs/adr/0029-agent-registry-api-migration.md.
"""

import datetime
import json
import os

PREVIEW_SURFACE = "preview-2026-07-08"
GA_SURFACE = "ga-2026-08-06"
_SURFACE_SERVICE_NAMES = {
    PREVIEW_SURFACE: "bedrock-agentcore-control",
    GA_SURFACE: "agent-registry-control",
}

# Published migration milestones (registry-faq, retrieved 2026-07-29)
GA_LAUNCH_DATE = datetime.date(2026, 8, 6)
PREVIEW_SHUTDOWN_DATE = datetime.date(2026, 9, 17)

MIGRATION_RUNBOOK = "assets/docs/adr/0029-agent-registry-api-migration.md"

# The env var is the single knob the namespace migration turns.
SURFACE_ENV_VAR = "GIP_AGENT_REGISTRY_API_SURFACE"


def service_name_for_surface(surface: str) -> str:
    """Resolve the boto3 service name for a pinned API surface."""
    try:
        return _SURFACE_SERVICE_NAMES[surface]
    except KeyError:
        raise ValueError(
            f"Unknown Agent Registry API surface {surface!r}; "
            f"expected one of {sorted(_SURFACE_SERVICE_NAMES)} "
            f"(set via {SURFACE_ENV_VAR}). See {MIGRATION_RUNBOOK}."
        ) from None


API_SURFACE = os.environ.get(SURFACE_ENV_VAR, GA_SURFACE)
SERVICE_NAME = service_name_for_surface(API_SURFACE)

# Record typing for skills: preview descriptorType vs GA recordType.
AGENT_SKILLS_DESCRIPTOR_TYPE = "AGENT_SKILLS"
GA_SKILL_RECORD_TYPE = "SKILL"


def preview_lifecycle_check(today: datetime.date | None = None, explicit_surface: bool | None = None) -> str | None:
    """Fail-visible detection of the published preview API break.

    Returns a warning string once GA launches (2026-08-06); raises once the
    preview endpoints are shut down (2026-09-17) unless the operator has
    explicitly pinned the preview surface via the env var (escape hatch in
    case AWS extends the deadline).
    """
    today = today or datetime.date.today()
    if explicit_surface is None:
        explicit_surface = SURFACE_ENV_VAR in os.environ
    if today < GA_LAUNCH_DATE:
        return None
    message = (
        f"AWS Agent Registry preview namespace (bedrock-agentcore) is being retired: "
        f"GA namespace launched {GA_LAUNCH_DATE.isoformat()}, preview endpoints shut down "
        f"{PREVIEW_SHUTDOWN_DATE.isoformat()}. Migrate now: set {SURFACE_ENV_VAR}={GA_SURFACE} "
        f"after completing the data migration. Runbook: {MIGRATION_RUNBOOK}"
    )
    if today >= PREVIEW_SHUTDOWN_DATE and not explicit_surface:
        raise RuntimeError(f"Preview Agent Registry API surface is past its shutdown date. {message}")
    return message


def _record_id_from_arn(value: str | None) -> str | None:
    if not value:
        return None
    if "/record/" in value:
        return value.split("/record/", 1)[1].split("/", 1)[0]
    return value


class AgentRegistryClient:
    """Thin wrapper over the registry control plane (injectable for tests)."""

    def __init__(self, region: str | None = None, client=None, surface: str | None = None):
        self._surface = surface or API_SURFACE
        service_name = service_name_for_surface(self._surface)
        if client is None:
            import boto3
            from botocore.exceptions import UnknownServiceError

            if self._surface == PREVIEW_SURFACE:
                warning = preview_lifecycle_check()
                if warning:
                    print(f"WARNING: {warning}")
            try:
                client = boto3.client(service_name, region_name=region)
            except UnknownServiceError as e:
                raise RuntimeError(
                    f"botocore does not know the service '{service_name}'. The GA "
                    f"agent-registry namespace requires a botocore release from "
                    f"{GA_LAUNCH_DATE.isoformat()} or later; upgrade boto3/botocore "
                    f"(or set {SURFACE_ENV_VAR}={PREVIEW_SURFACE} to fall back to "
                    f"the preview surface, which works until "
                    f"{PREVIEW_SHUTDOWN_DATE.isoformat()}). See {MIGRATION_RUNBOOK}."
                ) from e
        self._client = client

    def create_record(
        self,
        registry_id: str,
        name: str,
        record_version: str,
        skill_markdown: str,
        definition: dict,
        description: str = "",
    ) -> str:
        definition_json = json.dumps(definition, separators=(",", ":"))
        schema_version = definition.get("schemaVersion", "0.1.0")
        if self._surface == GA_SURFACE:
            # registry-faq "Change 2/3" (retrieved 2026-07-29): recordType
            # replaces descriptorType; name is the dedup key (unique with
            # recordVersion); displayName carries the old name semantics.
            resp = self._client.create_registry_record(
                registryId=registry_id,
                name=name,
                displayName=name,
                description=description,
                recordVersion=record_version,
                recordType=GA_SKILL_RECORD_TYPE,
                descriptors={
                    "agentSkillsDefinition": {
                        "data": definition_json,
                        "dataSchemaVersion": schema_version,
                        "additionalData": {"skillMd": {"data": skill_markdown}},
                    }
                },
            )
        else:
            resp = self._client.create_registry_record(
                registryId=registry_id,
                name=name,
                description=description,
                recordVersion=record_version,
                descriptorType=AGENT_SKILLS_DESCRIPTOR_TYPE,
                descriptors={
                    "agentSkills": {
                        "skillMd": {"inlineContent": skill_markdown},
                        "skillDefinition": {
                            "schemaVersion": schema_version,
                            "inlineContent": definition_json,
                        },
                    }
                },
            )
        return (
            resp.get("recordId") or _record_id_from_arn(resp.get("recordArn")) or resp.get("record", {}).get("recordId")
        )

    def list_records(
        self,
        registry_id: str,
        status: str | None = None,
        descriptor_type: str | None = AGENT_SKILLS_DESCRIPTOR_TYPE,
    ) -> list[dict]:
        records: list[dict] = []
        token = None
        while True:
            kwargs: dict = {"registryId": registry_id}
            if self._surface == GA_SURFACE:
                # registry-faq "Change 6" (retrieved 2026-07-29): one
                # structured filters parameter replaces discrete params.
                filters = []
                if descriptor_type:
                    record_type = (
                        GA_SKILL_RECORD_TYPE if descriptor_type == AGENT_SKILLS_DESCRIPTOR_TYPE else descriptor_type
                    )
                    filters.append({"name": "recordType", "values": [record_type]})
                if status:
                    filters.append({"name": "status", "values": [status]})
                if filters:
                    kwargs["filters"] = filters
            else:
                if descriptor_type:
                    kwargs["descriptorType"] = descriptor_type
                if status:
                    kwargs["status"] = status
            if token:
                kwargs["nextToken"] = token
            resp = self._client.list_registry_records(**kwargs)
            if not any(key in resp for key in ("registryRecords", "records", "items")):
                raise RuntimeError(f"ListRegistryRecords response missing records key; keys={sorted(resp)}")
            for record in resp.get("registryRecords", resp.get("records", resp.get("items", []))) or []:
                record_id = record.get("recordId") or _record_id_from_arn(record.get("recordArn"))
                if not record_id:
                    raise RuntimeError(
                        f"ListRegistryRecords returned a record summary without recordId: {sorted(record)}"
                    )
                record = {**record, "recordId": record_id}
                if record.get("descriptors"):
                    records.append(record)
                    continue
                records.append({**record, **self.get_record(registry_id, record_id)})
            token = resp.get("nextToken")
            if not token:
                break
        if status:
            missing_status = [r.get("recordId") or r.get("name", "?") for r in records if "status" not in r]
            if missing_status:
                raise RuntimeError(f"ListRegistryRecords returned record(s) without status: {missing_status}")
            records = [r for r in records if r["status"] == status]
        return records

    def submit_for_approval(self, registry_id: str, record_id: str) -> None:
        self._client.submit_registry_record_for_approval(registryId=registry_id, recordId=record_id)

    def get_record(self, registry_id: str, record_id: str) -> dict:
        return self._client.get_registry_record(registryId=registry_id, recordId=record_id)

    def update_skill_definition(self, registry_id: str, record_id: str, definition: dict) -> dict:
        """Patch only the skill definition on an existing record.

        Both surfaces use PATCH-style ``optionalValue`` wrappers; the GA
        shape was verified against the botocore agent-registry-control
        2025-12-01 service model on 2026-08-12 (ADR-0029 day-one step 3).
        Mirrors the Lambda twin in
        lambda-functions/skills_registry/registry_client.py.
        """
        definition_json = json.dumps(definition, separators=(",", ":"))
        schema_version = definition.get("schemaVersion", "0.1.0")
        if self._surface == GA_SURFACE:
            # GA wraps EVERY updatable level in optionalValue:
            # descriptors.optionalValue.agentSkillsDefinition.optionalValue.
            # {data.optionalValue, dataSchemaVersion.optionalValue}
            return self._client.update_registry_record(
                registryId=registry_id,
                recordId=record_id,
                descriptors={
                    "optionalValue": {
                        "agentSkillsDefinition": {
                            "optionalValue": {
                                "data": {"optionalValue": definition_json},
                                "dataSchemaVersion": {"optionalValue": schema_version},
                            }
                        }
                    }
                },
            )
        return self._client.update_registry_record(
            registryId=registry_id,
            recordId=record_id,
            descriptors={
                "optionalValue": {
                    "agentSkills": {
                        "optionalValue": {
                            "skillDefinition": {
                                "optionalValue": {
                                    "schemaVersion": schema_version,
                                    "inlineContent": definition_json,
                                }
                            }
                        }
                    }
                }
            },
        )

    def update_record_status(self, registry_id: str, record_id: str, status: str, reason: str = "") -> None:
        kwargs = {"registryId": registry_id, "recordId": record_id, "status": status, "statusReason": reason or "gip"}
        self._client.update_registry_record_status(**kwargs)
