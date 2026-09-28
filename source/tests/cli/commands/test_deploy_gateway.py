# ABOUTME: Contract tests for the upstream-only Claude Apps Gateway integration.

from unittest.mock import patch

from cleo.testers.command_tester import CommandTester

from governed_inference_platform.cli.commands.deploy import (
    UPSTREAM_GATEWAY_FETCH_SCRIPT,
    UPSTREAM_GATEWAY_REPOSITORY,
    UPSTREAM_GATEWAY_VENDOR_PATH,
    VALID_STACKS,
    DeployCommand,
    upstream_gateway_guidance,
)
from governed_inference_platform.cli.commands.destroy import DESTROYABLE_STACKS
from governed_inference_platform.config import Profile


def _profile(**overrides) -> Profile:
    data = {
        "name": "test",
        "provider_domain": "company.okta.com",
        "client_id": "client-id",
        "credential_storage": "session",
        "aws_region": "us-east-1",
        "identity_pool_name": "gip",
        "provider_type": "okta",
    }
    data.update(overrides)
    return Profile.from_dict(data)


def _run_deploy(stack: str, profile: Profile) -> int:
    with patch("governed_inference_platform.cli.commands.deploy.Config") as config:
        config.load.return_value.get_profile.return_value = profile
        config.load.return_value.active_profile = "test"
        tester = CommandTester(DeployCommand())
        return tester.execute(stack)


def test_legacy_profile_fields_still_load_for_migration():
    profile = _profile(
        gateway_enabled=True,
        gateway_image="example.invalid/gateway:legacy",
        gateway_stack_url="https://legacy.internal.example.com",
        cowork_config_delivery="bootstrap-device-code",
    )

    restored = Profile.from_dict(profile.to_dict())

    assert restored.gateway_enabled is True
    assert restored.gateway_image == "example.invalid/gateway:legacy"
    assert restored.gateway_stack_url == "https://legacy.internal.example.com"
    assert restored.cowork_config_delivery == "bootstrap-device-code"


def test_gateway_and_bootstrap_remain_destroyable_legacy_stack_names():
    assert "gateway" in VALID_STACKS
    assert "bootstrap" in VALID_STACKS
    assert "gateway" in DESTROYABLE_STACKS
    assert "bootstrap" in DESTROYABLE_STACKS


def test_gateway_deploy_is_tombstoned_to_exact_upstream_source(capsys):
    exit_code = _run_deploy("gateway", _profile(gateway_enabled=True))
    output = capsys.readouterr().out

    assert exit_code == 1
    assert UPSTREAM_GATEWAY_REPOSITORY in output
    assert f"{UPSTREAM_GATEWAY_VENDOR_PATH}/claude-apps-gateway" in output
    assert UPSTREAM_GATEWAY_FETCH_SCRIPT in output


def test_bootstrap_deploy_is_tombstoned_to_exact_upstream_source(capsys):
    exit_code = _run_deploy("bootstrap", _profile(cowork_config_delivery="bootstrap-device-code"))
    output = capsys.readouterr().out

    assert exit_code == 1
    assert f"{UPSTREAM_GATEWAY_VENDOR_PATH}/claude-apps-gateway-bootstrap" in output
    assert UPSTREAM_GATEWAY_FETCH_SCRIPT in output


def test_upstream_guidance_does_not_claim_gip_owns_deployment():
    message = upstream_gateway_guidance()

    assert "no longer maintains or deploys" in message
    assert "unmodified AWS Samples source" in message
    assert UPSTREAM_GATEWAY_FETCH_SCRIPT in message
    assert f"{UPSTREAM_GATEWAY_VENDOR_PATH}/UPSTREAM.json" in message
    assert "Git tree identities" in message
    assert "SHA-256 manifests" not in message
    assert "sync-claude-apps-gateway.sh" not in message
