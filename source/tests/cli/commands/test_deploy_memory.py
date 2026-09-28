# ABOUTME: Tests for the AgentCore Memory stack wiring (config + deploy/destroy CLI + init answers)
# ABOUTME: Covers Profile backward-compat, memory_preflight, CFN param derivation, destroy ordering, and answers-file validation

"""Unit tests for the memory stack plumbing (`gip deploy memory`, ADR-0016)."""

from unittest.mock import patch

import pytest
from cleo.testers.command_tester import CommandTester

from governed_inference_platform.cli.commands.deploy import (
    VALID_STACKS,
    DeployCommand,
    build_memory_params,
    memory_preflight,
)
from governed_inference_platform.cli.commands.destroy import DESTROYABLE_STACKS
from governed_inference_platform.cli.commands.init_answers import build_config_from_answers
from governed_inference_platform.config import Profile


def _memory_profile(**overrides) -> Profile:
    """A profile with web search + user memory enabled (Okta provider)."""
    data = {
        "name": "test",
        "provider_domain": "company.okta.com",
        "client_id": "0oa1example2",
        "credential_storage": "keyring",
        "aws_region": "us-east-1",
        "identity_pool_name": "gip-test",
        "provider_type": "okta",
        "web_search_enabled": True,
        "memory_enabled": True,
        "memory_user_enabled": True,
    }
    data.update(overrides)
    return Profile.from_dict(data)


# --- Backward compatibility ---


def test_profile_without_memory_fields_loads_with_defaults():
    """A profile saved before this feature loads with memory disabled."""
    legacy = {
        "name": "legacy",
        "provider_domain": "example.okta.com",
        "client_id": "abc",
        "credential_storage": "session",
        "aws_region": "us-west-2",
        "identity_pool_name": "gip",
    }
    profile = Profile.from_dict(legacy)
    assert profile.memory_enabled is False
    assert profile.memory_user_enabled is False
    assert profile.memory_org_enabled is False
    assert profile.memory_mode == "extracted-only"
    assert profile.memory_raw_event_retention_days == 30
    assert profile.memory_kms_key_arn is None
    assert profile.memory_org_write_groups == []
    assert profile.memory_deploy_gate == "log-only"
    assert profile.memory_id == ""


def test_profile_memory_roundtrip():
    profile = _memory_profile(
        memory_org_enabled=True,
        memory_org_write_groups=["memory-admins"],
        memory_id="mem-xyz",
    )
    restored = Profile.from_dict(profile.to_dict())
    assert restored.memory_enabled is True
    assert restored.memory_org_write_groups == ["memory-admins"]
    assert restored.memory_id == "mem-xyz"


# --- Stack registration + destroy ordering ---


def test_valid_stacks_includes_memory():
    assert "memory" in VALID_STACKS


def test_destroyable_stacks_includes_memory():
    assert "memory" in DESTROYABLE_STACKS


def test_destroy_order_memory_before_websearch():
    """The memory target rides on the websearch gateway: it must be destroyed
    before the gateway it attaches to."""
    assert DESTROYABLE_STACKS.index("memory") < DESTROYABLE_STACKS.index("websearch")


# --- Preflight ---


def test_preflight_ok_for_user_memory_profile():
    ok, msg = memory_preflight(_memory_profile())
    assert ok is True
    assert msg is None


def test_preflight_requires_web_search():
    ok, msg = memory_preflight(_memory_profile(web_search_enabled=False))
    assert ok is False
    assert "web search" in msg.lower()


def test_preflight_requires_deployable_websearch():
    """A broken websearch prerequisite (unsupported provider) blocks memory too."""
    ok, msg = memory_preflight(_memory_profile(provider_type=None))
    assert ok is False
    assert "not deployable" in msg


def test_preflight_requires_at_least_one_scope():
    ok, msg = memory_preflight(_memory_profile(memory_user_enabled=False, memory_org_enabled=False))
    assert ok is False
    assert "at least one scope" in msg.lower()


@pytest.mark.parametrize(
    "overrides,fragment",
    [
        ({"memory_mode": "everything"}, "memory_mode"),
        ({"memory_mode": "full", "memory_raw_event_retention_days": 2}, "3-365"),
        ({"memory_mode": "full", "memory_raw_event_retention_days": 400}, "3-365"),
        ({"memory_deploy_gate": "open"}, "memory_deploy_gate"),
    ],
)
def test_preflight_rejects_out_of_contract_values(overrides, fragment):
    ok, msg = memory_preflight(_memory_profile(**overrides))
    assert ok is False
    assert fragment in msg


def test_preflight_ignores_retention_in_extracted_only_mode():
    """Retention is a full-mode knob: extracted-only pins the 3-day floor."""
    ok, _ = memory_preflight(_memory_profile(memory_mode="extracted-only", memory_raw_event_retention_days=1))
    assert ok is True


# --- CFN parameter derivation ---

GATEWAY_ID = "w3-websearch-gw-abcdef1234"
GATEWAY_ROLE = "arn:aws:iam::123456789012:role/ws-gateway-exec"


def _params_dict(profile) -> dict:
    return dict(p.split("=", 1) for p in build_memory_params(profile, GATEWAY_ID, GATEWAY_ROLE))


def test_build_memory_params_baseline():
    params = _params_dict(_memory_profile())
    assert params["IdentityPoolName"] == "gip-test"
    assert params["GatewayIdentifier"] == GATEWAY_ID
    assert params["GatewayExecutionRoleArn"] == GATEWAY_ROLE
    assert params["EnableUserMemory"] == "true"
    assert params["EnableOrgMemory"] == "false"
    assert params["MemoryMode"] == "extracted-only"
    assert params["RawEventRetentionDays"] == "30"
    assert params["DeployGate"] == "log-only"
    # Optional params are omitted when unset (template defaults apply).
    assert "KmsKeyArn" not in params
    assert "OrgMemoryWriteGroups" not in params


def test_build_memory_params_full_mode_and_org():
    profile = _memory_profile(
        memory_org_enabled=True,
        memory_mode="full",
        memory_raw_event_retention_days=90,
        memory_kms_key_arn="arn:aws:kms:us-east-1:123456789012:key/abc",
        memory_org_write_groups=["memory-admins", "kb-editors"],
        memory_deploy_gate="active",
    )
    params = _params_dict(profile)
    assert params["EnableOrgMemory"] == "true"
    assert params["MemoryMode"] == "full"
    assert params["RawEventRetentionDays"] == "90"
    assert params["DeployGate"] == "active"
    assert params["KmsKeyArn"] == "arn:aws:kms:us-east-1:123456789012:key/abc"
    assert params["OrgMemoryWriteGroups"] == "memory-admins,kb-editors"


# --- CLI accepts the stack type / gates on configuration ---


def _run_deploy(profile, stack_arg="memory"):
    with patch("governed_inference_platform.cli.commands.deploy.Config") as MockConfig:
        MockConfig.load.return_value.get_profile.return_value = profile
        MockConfig.load.return_value.active_profile = "test"
        tester = CommandTester(DeployCommand())
        return tester.execute(stack_arg)


def test_memory_stack_arg_accepted_but_disabled_profile_errors(capsys):
    """'memory' is a valid stack argument; a disabled profile exits non-zero
    with guidance instead of 'Unknown stack'."""
    exit_code = _run_deploy(_memory_profile(memory_enabled=False))
    output = capsys.readouterr().out
    assert exit_code == 1
    assert "Unknown stack" not in output
    assert "not enabled" in output


def test_memory_deploy_blocked_without_web_search(capsys):
    exit_code = _run_deploy(_memory_profile(web_search_enabled=False))
    output = capsys.readouterr().out
    assert exit_code == 1
    assert "web search" in output.lower()


# --- Answers file (init --from-file) plumbing ---


def _answers(memory: dict, web_search: dict | None = None) -> dict:
    answers = {
        "okta": {"domain": "company.okta.com", "client_id": "0oa0000000000000000"},
        "memory": memory,
    }
    if web_search is not None:
        answers["web_search"] = web_search
    return answers


def test_answers_memory_defaults_off():
    config, errors = build_config_from_answers(_answers({}))
    assert errors == []
    assert config["memory"]["enabled"] is False


def test_answers_memory_enabled_follows_scopes():
    config, errors = build_config_from_answers(_answers({"user_enabled": True}, web_search={"enabled": True}))
    assert errors == []
    assert config["memory"]["enabled"] is True
    assert config["memory"]["mode"] == "extracted-only"


def test_answers_memory_requires_web_search():
    _, errors = build_config_from_answers(_answers({"user_enabled": True}))
    assert any("web_search.enabled" in e for e in errors)


def test_answers_memory_enabled_without_scopes_errors():
    _, errors = build_config_from_answers(_answers({"enabled": True}, web_search={"enabled": True}))
    assert any("at least one of" in e for e in errors)


@pytest.mark.parametrize(
    "memory,fragment",
    [
        ({"user_enabled": True, "mode": "everything"}, "memory.mode"),
        ({"user_enabled": True, "raw_event_retention_days": 1}, "memory.raw_event_retention_days"),
        ({"user_enabled": True, "raw_event_retention_days": 400}, "memory.raw_event_retention_days"),
        ({"org_enabled": True, "org_write_groups": [""]}, "memory.org_write_groups"),
    ],
)
def test_answers_memory_validation_errors(memory, fragment):
    _, errors = build_config_from_answers(_answers(memory, web_search={"enabled": True}))
    assert any(fragment in e for e in errors), errors
