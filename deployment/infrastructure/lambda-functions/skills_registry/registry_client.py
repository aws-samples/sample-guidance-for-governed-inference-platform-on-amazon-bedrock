# ABOUTME: Single isolation point for every AWS Agent Registry API call (both API surfaces)
# ABOUTME: Pins the preview (bedrock-agentcore) and GA (agent-registry) surfaces; one env-switched cutover

"""Agent Registry client — the ONLY module that talks to the registry APIs.

TWO PINNED API SURFACES, selected by one switch point (the
``GIP_AGENT_REGISTRY_API_SURFACE`` environment variable, default GA since
the 2026-08-12 day-one execution of ADR-0029):

``preview-2026-07-08`` — boto3 client("bedrock-agentcore-control")
    create_registry, get_registry, update_registry, delete_registry,
    list_registries, create_registry_record, get_registry_record,
    update_registry_record, delete_registry_record, list_registry_records,
    submit_registry_record_for_approval, update_registry_record_status
  Sources (retrieved 2026-07-08):
    https://docs.aws.amazon.com/cli/latest/reference/bedrock-agentcore-control/create-registry.html
    https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-create-manage-records.html

``ga-2026-08-06`` — boto3 client("agent-registry-control")  (DEFAULT)
  Launched 2026-08-06; requires a botocore release that ships the
  ``agent-registry-control`` service model (verified: botocore 1.43.69,
  model ``agent-registry-control/2025-12-01``). Operation names are
  unchanged; the schema changes are:
    - records: ``descriptorType`` removed -> top-level ``recordType="SKILL"``;
      old ``name`` -> ``displayName``; new required dedup ``name`` (unique with
      ``recordVersion``); ``descriptors.agentSkills.{skillDefinition,skillMd}``
      -> ``descriptors.agentSkillsDefinition.{data, dataSchemaVersion,
      additionalData.skillMd.data}``; ``inlineContent`` -> ``data``;
      ``schemaVersion`` -> ``dataSchemaVersion``
    - list APIs: discrete filter params -> ``filters=[{"name","values"}]``
      (filter names pinned by the model enum: name, status, recordType)
    - registry entity: ``authorizerType``/``authorizerConfiguration`` wrapped
      in ``discoveryConfiguration``
    - UpdateRegistryRecord keeps PATCH-style ``optionalValue`` wrappers at
      EVERY level of the GA request (verified against the botocore 1.43.69
      service model on 2026-08-12, ADR-0029 day-one execution):
      ``descriptors.optionalValue.agentSkillsDefinition.optionalValue.
      {data.optionalValue, dataSchemaVersion.optionalValue}``
    - UpdateRegistry's GA ``description`` is wrapped the same way:
      ``description={"optionalValue": ...}`` (resolves ADR-0029 unknown 4)
  Sources: "Comprehensive registry migration guide" (retrieved 2026-07-29):
    https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-faq.html
    and the installed botocore agent-registry-control/2025-12-01 model
    (inspected 2026-08-12).

Old-namespace endpoints shut down 2026-09-17 (same source). GA registries are
NEW resources: data does not carry over — cutover requires the AWS migration
tooling or the git+S3 re-publish/re-approve replay. Full day-one procedure:
assets/docs/adr/0029-agent-registry-api-migration.md.

The CLI mirrors this module in
source/governed_inference_platform/cli/utils/agent_registry.py — keep the two
in sync. Registry state is treated as re-creatable: content lives in git + S3,
the registry only owns approval state.
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


def service_name_for_surface(surface):
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

# Record typing for skills.
# Preview: descriptorType (registry-supported-record-types, retrieved 2026-07-08)
AGENT_SKILLS_DESCRIPTOR_TYPE = "AGENT_SKILLS"
# GA: recordType (registry-faq "Change 2/3", retrieved 2026-07-29)
GA_SKILL_RECORD_TYPE = "SKILL"


def preview_lifecycle_check(today=None, explicit_surface=None):
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


def _registry_id_from_arn(value):
    if not value:
        return None
    if ":registry/" in value:
        return value.split(":registry/", 1)[1].split("/", 1)[0]
    return value


def _record_id_from_arn(value):
    if not value:
        return None
    if "/record/" in value:
        return value.split("/record/", 1)[1].split("/", 1)[0]
    return value


class RegistryClient:
    """Thin wrapper over the registry control plane.

    Accepts an injected boto3-compatible client for tests; creates the real
    client lazily otherwise (boto3 import stays inside so unit tests never
    need AWS credentials).
    """

    def __init__(self, region=None, client=None, surface=None):
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
                client = boto3.client(
                    service_name, region_name=region or os.environ.get("AWS_REGION")
                )
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

    # ---------------------------------------------------------------
    # Registry lifecycle
    # ---------------------------------------------------------------

    def find_registry_by_name(self, name):
        """Return the registry summary dict whose name matches, or None."""
        token = None
        while True:
            kwargs = {}
            if token:
                kwargs["nextToken"] = token
            resp = self._client.list_registries(**kwargs)
            for reg in resp.get("registries", resp.get("items", [])) or []:
                if reg.get("name") == name:
                    return reg
            token = resp.get("nextToken")
            if not token:
                return None

    def create_registry(self, name, description="", authorizer_type="AWS_IAM"):
        """Create a registry; returns its registryId."""
        kwargs = {"name": name, "description": description}
        if self._surface == GA_SURFACE:
            # GA wraps the authorizer under discoveryConfiguration
            # (registry-faq "Change 1", retrieved 2026-07-29).
            kwargs["discoveryConfiguration"] = {"authorizerType": authorizer_type}
        else:
            kwargs["authorizerType"] = authorizer_type
        resp = self._client.create_registry(**kwargs)
        registry_id = (
            resp.get("registryId")
            or _registry_id_from_arn(resp.get("registryArn"))
            or resp.get("registry", {}).get("registryId")
        )
        if not registry_id:
            raise RuntimeError(
                "CreateRegistry response did not include a registry identifier"
            )
        return registry_id

    def get_registry(self, registry_id):
        return self._client.get_registry(registryId=registry_id)

    def update_registry(self, registry_id, description):
        if self._surface == GA_SURFACE:
            # GA UpdateRegistry wraps PATCH-able fields in optionalValue
            # (agent-registry-control/2025-12-01 model, inspected 2026-08-12).
            self._client.update_registry(
                registryId=registry_id, description={"optionalValue": description}
            )
            return
        self._client.update_registry(registryId=registry_id, description=description)

    def delete_registry(self, registry_id):
        self._client.delete_registry(registryId=registry_id)

    # ---------------------------------------------------------------
    # Records
    # ---------------------------------------------------------------

    def create_record(
        self,
        registry_id,
        name,
        record_version,
        skill_markdown,
        definition,
        description="",
    ):
        """Create a skill record (Manual source — skill records do not
        support synchronization on either surface)."""
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
            resp.get("recordId")
            or _record_id_from_arn(resp.get("recordArn"))
            or resp.get("record", {}).get("recordId")
        )

    def get_record(self, registry_id, record_id):
        return self._client.get_registry_record(
            registryId=registry_id, recordId=record_id
        )

    def list_records(
        self, registry_id, status=None, descriptor_type=AGENT_SKILLS_DESCRIPTOR_TYPE
    ):
        """Return all records, following pagination. ``status`` filters
        client-side too, in case the server-side filter shape shifts."""
        records = []
        token = None
        while True:
            kwargs = {"registryId": registry_id}
            if self._surface == GA_SURFACE:
                # registry-faq "Change 6" (retrieved 2026-07-29): one
                # structured filters parameter replaces discrete params.
                filters = []
                if descriptor_type:
                    record_type = (
                        GA_SKILL_RECORD_TYPE
                        if descriptor_type == AGENT_SKILLS_DESCRIPTOR_TYPE
                        else descriptor_type
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
                raise RuntimeError(
                    f"ListRegistryRecords response missing records key; keys={sorted(resp)}"
                )
            for record in (
                resp.get("registryRecords", resp.get("records", resp.get("items", [])))
                or []
            ):
                record_id = record.get("recordId") or _record_id_from_arn(
                    record.get("recordArn")
                )
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
            missing_status = [
                r.get("recordId") or r.get("name", "?")
                for r in records
                if "status" not in r
            ]
            if missing_status:
                raise RuntimeError(
                    f"ListRegistryRecords returned record(s) without status: {missing_status}"
                )
            records = [r for r in records if r["status"] == status]
        return records

    def submit_for_approval(self, registry_id, record_id):
        self._client.submit_registry_record_for_approval(
            registryId=registry_id, recordId=record_id
        )

    def update_skill_definition(self, registry_id, record_id, definition):
        """Patch only the skill definition on an existing record.

        Both surfaces use PATCH-style ``optionalValue`` wrappers; the GA
        shape was verified against the botocore agent-registry-control
        2025-12-01 service model on 2026-08-12 (ADR-0029 day-one step 3).
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
                                "dataSchemaVersion": {
                                    "optionalValue": schema_version
                                },
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

    def update_record_status(self, registry_id, record_id, status, reason=""):
        """Approve/reject: status is APPROVED or REJECTED."""
        kwargs = {
            "registryId": registry_id,
            "recordId": record_id,
            "status": status,
            "statusReason": reason or "gip",
        }
        self._client.update_registry_record_status(**kwargs)
