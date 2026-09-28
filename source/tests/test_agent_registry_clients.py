import datetime
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import botocore.session
import pytest

from governed_inference_platform.cli.utils.agent_registry import (
    GA_SURFACE,
    PREVIEW_SURFACE,
    AgentRegistryClient,
    preview_lifecycle_check,
    service_name_for_surface,
)

LAMBDA_REGISTRY_CLIENT = (
    Path(__file__).resolve().parents[2]
    / "deployment"
    / "infrastructure"
    / "lambda-functions"
    / "skills_registry"
    / "registry_client.py"
)


def _load_lambda_registry_client():
    spec = importlib.util.spec_from_file_location("skills_registry_client_under_test", LAMBDA_REGISTRY_CLIENT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["skills_registry_client_under_test"] = module
    spec.loader.exec_module(module)
    return module


def test_cli_create_record_uses_agent_skills_shape_and_returns_record_id():
    boto = MagicMock()
    boto.create_registry_record.return_value = {
        "recordArn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:registry/abcdefghijkl/record/ABCDEF123456",
        "status": "DRAFT",
    }
    client = AgentRegistryClient(client=boto, surface=PREVIEW_SURFACE)

    record_id = client.create_record(
        "abcdefghijkl",
        "code-review",
        "1.2.0",
        "# skill",
        {"schemaVersion": "0.1.0", "_meta": {"io.gip.skill/v1": {"name": "code-review"}}},
        "desc",
    )

    assert record_id == "ABCDEF123456"
    kwargs = boto.create_registry_record.call_args.kwargs
    assert kwargs["descriptorType"] == "AGENT_SKILLS"
    agent_skills = kwargs["descriptors"]["agentSkills"]
    assert agent_skills["skillMd"] == {"inlineContent": "# skill"}
    assert (
        json.loads(agent_skills["skillDefinition"]["inlineContent"])["_meta"]["io.gip.skill/v1"]["name"]
        == "code-review"
    )


def test_cli_list_records_enriches_registry_record_summaries():
    boto = MagicMock()
    boto.list_registry_records.return_value = {
        "registryRecords": [{"recordId": "ABCDEF123456", "name": "code-review", "status": "APPROVED"}]
    }
    boto.get_registry_record.return_value = {
        "recordId": "ABCDEF123456",
        "descriptors": {"agentSkills": {"skillDefinition": {"inlineContent": '{"_meta": {}}'}}},
        "status": "APPROVED",
    }
    client = AgentRegistryClient(client=boto)

    records = client.list_records("abcdefghijkl", status="APPROVED")

    assert records[0]["descriptors"]["agentSkills"]["skillDefinition"]
    boto.get_registry_record.assert_called_once_with(registryId="abcdefghijkl", recordId="ABCDEF123456")


def test_cli_update_record_status_uses_status_reason():
    boto = MagicMock()
    client = AgentRegistryClient(client=boto)

    client.update_record_status("abcdefghijkl", "ABCDEF123456", "APPROVED", reason="reviewed")

    boto.update_registry_record_status.assert_called_once_with(
        registryId="abcdefghijkl",
        recordId="ABCDEF123456",
        status="APPROVED",
        statusReason="reviewed",
    )


def test_cli_list_records_fails_when_summary_cannot_be_enriched():
    boto = MagicMock()
    boto.list_registry_records.return_value = {
        "registryRecords": [{"name": "code-review", "status": "APPROVED", "descriptors": {"agentSkills": {}}}]
    }
    client = AgentRegistryClient(client=boto)

    try:
        client.list_records("abcdefghijkl", status="APPROVED")
    except RuntimeError as e:
        assert "without recordId" in str(e)
    else:
        raise AssertionError("expected RuntimeError")


def test_cli_list_records_fails_when_filtered_record_has_no_status():
    boto = MagicMock()
    boto.list_registry_records.return_value = {"registryRecords": [{"recordId": "ABCDEF123456", "name": "code-review"}]}
    boto.get_registry_record.return_value = {
        "recordId": "ABCDEF123456",
        "descriptors": {"agentSkills": {"skillDefinition": {"inlineContent": '{"_meta": {}}'}}},
    }
    client = AgentRegistryClient(client=boto)

    try:
        client.list_records("abcdefghijkl", status="APPROVED")
    except RuntimeError as e:
        assert "without status" in str(e)
    else:
        raise AssertionError("expected RuntimeError")


def test_cli_list_records_fails_when_response_has_no_records_key():
    boto = MagicMock()
    boto.list_registry_records.return_value = {"things": []}
    client = AgentRegistryClient(client=boto)

    try:
        client.list_records("abcdefghijkl", status="APPROVED")
    except RuntimeError as e:
        assert "missing records key" in str(e)
    else:
        raise AssertionError("expected RuntimeError")


def test_lambda_registry_create_registry_parses_registry_id_from_arn():
    module = _load_lambda_registry_client()
    boto = MagicMock()
    boto.create_registry.return_value = {
        "registryArn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:registry/abcdefghijkl"
    }
    client = module.RegistryClient(client=boto, surface=module.PREVIEW_SURFACE)

    assert client.create_registry("gip-skills") == "abcdefghijkl"


def test_lambda_registry_create_registry_requires_resolved_id():
    module = _load_lambda_registry_client()
    boto = MagicMock()
    boto.create_registry.return_value = {}
    client = module.RegistryClient(client=boto)

    try:
        client.create_registry("gip-skills")
    except RuntimeError as e:
        assert "registry identifier" in str(e)
    else:
        raise AssertionError("expected RuntimeError")


def test_lambda_registry_list_records_enriches_summaries():
    module = _load_lambda_registry_client()
    boto = MagicMock()
    boto.list_registry_records.return_value = {
        "registryRecords": [
            {
                "recordArn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:registry/abcdefghijkl/record/ABCDEF123456",
                "status": "APPROVED",
            }
        ]
    }
    boto.get_registry_record.return_value = {
        "recordId": "ABCDEF123456",
        "descriptors": {"agentSkills": {"skillDefinition": {"inlineContent": '{"_meta": {}}'}}},
        "status": "APPROVED",
    }
    client = module.RegistryClient(client=boto)

    records = client.list_records("abcdefghijkl", status="APPROVED")

    assert records[0]["recordId"] == "ABCDEF123456"
    boto.get_registry_record.assert_called_once_with(registryId="abcdefghijkl", recordId="ABCDEF123456")


def test_lambda_registry_list_records_fails_when_summary_cannot_be_enriched():
    module = _load_lambda_registry_client()
    boto = MagicMock()
    boto.list_registry_records.return_value = {
        "registryRecords": [{"name": "code-review", "status": "APPROVED", "descriptors": {"agentSkills": {}}}]
    }
    client = module.RegistryClient(client=boto)

    try:
        client.list_records("abcdefghijkl", status="APPROVED")
    except RuntimeError as e:
        assert "without recordId" in str(e)
    else:
        raise AssertionError("expected RuntimeError")


# ---------------------------------------------------------------------------
# GA surface (agent-registry namespace, launches 2026-08-06) — registry-faq
# "Comprehensive registry migration guide", retrieved 2026-07-29.
# ---------------------------------------------------------------------------


def test_surface_service_names_are_pinned():
    assert service_name_for_surface(PREVIEW_SURFACE) == "bedrock-agentcore-control"
    assert service_name_for_surface(GA_SURFACE) == "agent-registry-control"
    with pytest.raises(ValueError, match="Unknown Agent Registry API surface"):
        service_name_for_surface("bogus")


def test_installed_botocore_has_ga_agent_registry_model():
    assert "agent-registry-control" in botocore.session.get_session().get_available_services()


def test_lambda_and_cli_surface_constants_stay_in_sync():
    module = _load_lambda_registry_client()
    assert module.PREVIEW_SURFACE == PREVIEW_SURFACE
    assert module.GA_SURFACE == GA_SURFACE
    assert module.service_name_for_surface(GA_SURFACE) == service_name_for_surface(GA_SURFACE)
    assert module.GA_LAUNCH_DATE == datetime.date(2026, 8, 6)
    assert module.PREVIEW_SHUTDOWN_DATE == datetime.date(2026, 9, 17)


def test_preview_lifecycle_check_stages():
    assert preview_lifecycle_check(today=datetime.date(2026, 7, 29), explicit_surface=False) is None
    warning = preview_lifecycle_check(today=datetime.date(2026, 8, 6), explicit_surface=False)
    assert "2026-09-17" in warning and "GIP_AGENT_REGISTRY_API_SURFACE" in warning
    with pytest.raises(RuntimeError, match="past its shutdown date"):
        preview_lifecycle_check(today=datetime.date(2026, 9, 17), explicit_surface=False)
    # Explicitly pinning the preview surface is the operator escape hatch.
    assert "shut down" in preview_lifecycle_check(today=datetime.date(2026, 9, 17), explicit_surface=True)


def test_cli_ga_create_record_uses_skill_record_type_and_flat_descriptors():
    boto = MagicMock()
    boto.create_registry_record.return_value = {
        "recordArn": "arn:aws:agent-registry:us-east-1:123456789012:registry/abcdefghijkl/record/ABCDEF123456",
        "status": "DRAFT",
    }
    client = AgentRegistryClient(client=boto, surface=GA_SURFACE)

    record_id = client.create_record(
        "abcdefghijkl",
        "code-review",
        "1.2.0",
        "# skill",
        {"schemaVersion": "0.1.0", "_meta": {"io.gip.skill/v1": {"name": "code-review"}}},
        "desc",
    )

    assert record_id == "ABCDEF123456"
    kwargs = boto.create_registry_record.call_args.kwargs
    assert "descriptorType" not in kwargs
    assert kwargs["recordType"] == "SKILL"
    assert kwargs["name"] == "code-review"
    assert kwargs["displayName"] == "code-review"
    assert kwargs["recordVersion"] == "1.2.0"
    descriptor = kwargs["descriptors"]["agentSkillsDefinition"]
    assert descriptor["dataSchemaVersion"] == "0.1.0"
    assert json.loads(descriptor["data"])["_meta"]["io.gip.skill/v1"]["name"] == "code-review"
    assert descriptor["additionalData"]["skillMd"] == {"data": "# skill"}


def test_cli_ga_list_records_uses_structured_filters():
    boto = MagicMock()
    boto.list_registry_records.return_value = {
        "registryRecords": [
            {
                "recordId": "ABCDEF123456",
                "name": "code-review",
                "displayName": "code-review",
                "status": "APPROVED",
                "descriptors": {"agentSkillsDefinition": {"data": '{"_meta": {}}'}},
            }
        ]
    }
    client = AgentRegistryClient(client=boto, surface=GA_SURFACE)

    records = client.list_records("abcdefghijkl", status="APPROVED")

    assert records[0]["recordId"] == "ABCDEF123456"
    kwargs = boto.list_registry_records.call_args.kwargs
    assert "descriptorType" not in kwargs and "status" not in kwargs
    assert kwargs["filters"] == [
        {"name": "recordType", "values": ["SKILL"]},
        {"name": "status", "values": ["APPROVED"]},
    ]


def test_cli_preview_list_records_keeps_discrete_filter_params():
    boto = MagicMock()
    boto.list_registry_records.return_value = {"registryRecords": []}
    client = AgentRegistryClient(client=boto, surface=PREVIEW_SURFACE)

    client.list_records("abcdefghijkl", status="APPROVED")

    kwargs = boto.list_registry_records.call_args.kwargs
    assert kwargs["descriptorType"] == "AGENT_SKILLS"
    assert kwargs["status"] == "APPROVED"
    assert "filters" not in kwargs


def test_lambda_ga_create_record_matches_cli_shape():
    module = _load_lambda_registry_client()
    boto = MagicMock()
    boto.create_registry_record.return_value = {"recordId": "ABCDEF123456"}
    client = module.RegistryClient(client=boto, surface=module.GA_SURFACE)

    record_id = client.create_record(
        "abcdefghijkl", "code-review", "1.2.0", "# skill", {"schemaVersion": "0.1.0"}, "desc"
    )

    assert record_id == "ABCDEF123456"
    kwargs = boto.create_registry_record.call_args.kwargs
    assert kwargs["recordType"] == "SKILL"
    assert "descriptorType" not in kwargs
    assert kwargs["descriptors"]["agentSkillsDefinition"]["additionalData"]["skillMd"] == {"data": "# skill"}


def test_lambda_ga_create_registry_wraps_authorizer_in_discovery_configuration():
    module = _load_lambda_registry_client()
    boto = MagicMock()
    boto.create_registry.return_value = {
        "registryArn": "arn:aws:agent-registry:us-east-1:123456789012:registry/abcdefghijkl"
    }
    client = module.RegistryClient(client=boto, surface=module.GA_SURFACE)

    assert client.create_registry("gip-skills", "desc", "AWS_IAM") == "abcdefghijkl"
    kwargs = boto.create_registry.call_args.kwargs
    assert kwargs["discoveryConfiguration"] == {"authorizerType": "AWS_IAM"}
    assert "authorizerType" not in kwargs


def test_lambda_preview_create_registry_keeps_top_level_authorizer_type():
    module = _load_lambda_registry_client()
    boto = MagicMock()
    boto.create_registry.return_value = {"registryId": "abcdefghijkl"}
    client = module.RegistryClient(client=boto, surface=module.PREVIEW_SURFACE)

    client.create_registry("gip-skills", "desc", "AWS_IAM")
    kwargs = boto.create_registry.call_args.kwargs
    assert kwargs["authorizerType"] == "AWS_IAM"
    assert "discoveryConfiguration" not in kwargs


def test_lambda_ga_update_skill_definition_uses_verified_optional_value_wrappers():
    """GA UpdateRegistryRecord shape verified against the botocore
    agent-registry-control/2025-12-01 service model (2026-08-12, ADR-0029
    day-one step 3): optionalValue wrappers at every updatable level."""
    module = _load_lambda_registry_client()
    boto = MagicMock()
    client = module.RegistryClient(client=boto, surface=module.GA_SURFACE)
    definition = {"schemaVersion": "0.1.0", "_meta": {"io.gip.skill/v1": {"name": "code-review"}}}

    client.update_skill_definition("abcdefghijkl", "ABCDEF123456", definition)

    kwargs = boto.update_registry_record.call_args.kwargs
    assert set(kwargs) == {"registryId", "recordId", "descriptors"}
    fields = kwargs["descriptors"]["optionalValue"]["agentSkillsDefinition"]["optionalValue"]
    assert fields["dataSchemaVersion"] == {"optionalValue": "0.1.0"}
    assert json.loads(fields["data"]["optionalValue"]) == definition
    # skillMd is deliberately NOT patched — the definition is the only field
    # the approve path mutates.
    assert "additionalData" not in fields


def test_cli_ga_update_skill_definition_matches_lambda_shape():
    boto = MagicMock()
    client = AgentRegistryClient(client=boto, surface=GA_SURFACE)
    definition = {"schemaVersion": "0.1.0", "_meta": {"io.gip.skill/v1": {"name": "code-review"}}}

    client.update_skill_definition("abcdefghijkl", "ABCDEF123456", definition)

    kwargs = boto.update_registry_record.call_args.kwargs
    fields = kwargs["descriptors"]["optionalValue"]["agentSkillsDefinition"]["optionalValue"]
    assert fields["dataSchemaVersion"] == {"optionalValue": "0.1.0"}
    assert json.loads(fields["data"]["optionalValue"]) == definition


def test_cli_preview_update_skill_definition_keeps_preview_wrappers():
    boto = MagicMock()
    client = AgentRegistryClient(client=boto, surface=PREVIEW_SURFACE)
    definition = {"schemaVersion": "0.1.0"}

    client.update_skill_definition("abcdefghijkl", "ABCDEF123456", definition)

    kwargs = boto.update_registry_record.call_args.kwargs
    skill_definition = kwargs["descriptors"]["optionalValue"]["agentSkills"]["optionalValue"]["skillDefinition"]
    assert skill_definition["optionalValue"]["schemaVersion"] == "0.1.0"
    assert json.loads(skill_definition["optionalValue"]["inlineContent"]) == definition


def test_lambda_ga_update_registry_wraps_description_in_optional_value():
    """GA UpdateRegistry PATCHes description via an optionalValue wrapper
    (resolves ADR-0029 unknown 4; verified 2026-08-12)."""
    module = _load_lambda_registry_client()
    boto = MagicMock()
    client = module.RegistryClient(client=boto, surface=module.GA_SURFACE)

    client.update_registry("abcdefghijkl", "new description")

    boto.update_registry.assert_called_once_with(
        registryId="abcdefghijkl", description={"optionalValue": "new description"}
    )


def test_lambda_preview_update_registry_passes_description_through():
    module = _load_lambda_registry_client()
    boto = MagicMock()
    client = module.RegistryClient(client=boto, surface=module.PREVIEW_SURFACE)

    client.update_registry("abcdefghijkl", "new description")

    boto.update_registry.assert_called_once_with(registryId="abcdefghijkl", description="new description")


def test_default_surface_is_ga_when_env_var_unset(monkeypatch):
    """ADR-0029 day-one step (executed 2026-08-12): the default surface is GA."""
    monkeypatch.delenv("GIP_AGENT_REGISTRY_API_SURFACE", raising=False)
    lambda_module = _load_lambda_registry_client()
    assert lambda_module.API_SURFACE == lambda_module.GA_SURFACE

    import governed_inference_platform.cli.utils.agent_registry as cli_module

    reloaded = importlib.reload(cli_module)
    try:
        assert reloaded.API_SURFACE == reloaded.GA_SURFACE
        assert reloaded.SERVICE_NAME == "agent-registry-control"
    finally:
        importlib.reload(cli_module)


def test_env_var_still_pins_preview_surface(monkeypatch):
    monkeypatch.setenv("GIP_AGENT_REGISTRY_API_SURFACE", PREVIEW_SURFACE)
    lambda_module = _load_lambda_registry_client()
    assert lambda_module.API_SURFACE == lambda_module.PREVIEW_SURFACE
    assert lambda_module.SERVICE_NAME == "bedrock-agentcore-control"


def test_missing_ga_service_model_error_names_the_preview_fallback(monkeypatch):
    """With GA the default, the botocore-too-old error must tell the operator
    how to pin the preview surface explicitly (not 'unset the env var')."""
    module = _load_lambda_registry_client()
    import boto3
    from botocore.exceptions import UnknownServiceError

    def _raise(*args, **kwargs):
        raise UnknownServiceError(service_name="agent-registry-control", known_service_names="s3")

    monkeypatch.delenv("GIP_AGENT_REGISTRY_API_SURFACE", raising=False)
    monkeypatch.setattr(boto3, "client", _raise)
    with pytest.raises(RuntimeError) as excinfo:
        module.RegistryClient(surface=module.GA_SURFACE)
    message = str(excinfo.value)
    assert "GIP_AGENT_REGISTRY_API_SURFACE=preview-2026-07-08" in message
    assert "2026-09-17" in message


def test_lambda_ga_list_records_uses_structured_filters():
    module = _load_lambda_registry_client()
    boto = MagicMock()
    boto.list_registry_records.return_value = {"registryRecords": []}
    client = module.RegistryClient(client=boto, surface=module.GA_SURFACE)

    client.list_records("abcdefghijkl", status="APPROVED")

    kwargs = boto.list_registry_records.call_args.kwargs
    assert kwargs["filters"] == [
        {"name": "recordType", "values": ["SKILL"]},
        {"name": "status", "values": ["APPROVED"]},
    ]
