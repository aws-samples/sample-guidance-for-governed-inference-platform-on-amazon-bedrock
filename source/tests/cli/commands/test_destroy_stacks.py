# ABOUTME: Tests for destroy command stack coverage and skip logic
# ABOUTME: Ensures destroy tears down every stack that deploy can create

"""Tests that `gip destroy` covers all deployable stacks.

Regression coverage for the gap where `distribution`, `codebuild`, and
`cowork-dashboard` stacks were deployed but never destroyed, leaving
orphaned S3/IAM, CodeBuild projects, and dashboards behind.
"""

import re
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cleo.testers.command_tester import CommandTester
from rich.console import Console

from governed_inference_platform.cli.commands.destroy import (
    DESTROYABLE_STACKS,
    DestroyCommand,
    _matches_legacy_stack_template,
)
from governed_inference_platform.config import Profile

DEPLOY_SOURCE = Path(__file__).resolve().parents[3] / "governed_inference_platform" / "cli" / "commands" / "deploy.py"


def _deployable_stack_types() -> set[str]:
    """Extract every stack type deploy.py can append to its deploy list."""
    source = DEPLOY_SOURCE.read_text(encoding="utf-8")
    return set(re.findall(r'stacks_to_deploy\.append\(\(\s*"([a-z0-9-]+)"', source))


class TestDestroyStackCoverage:
    """Every stack deploy can create must also be destroyable."""

    def test_destroy_covers_every_deployable_stack(self):
        deployable = _deployable_stack_types()
        missing = deployable - set(DESTROYABLE_STACKS)
        assert not missing, f"destroy is missing deployable stacks: {sorted(missing)}"

    def test_only_legacy_cleanup_stacks_are_destroy_only(self):
        deployable = _deployable_stack_types()
        phantom = set(DESTROYABLE_STACKS) - deployable
        assert phantom == {"bootstrap", "gateway"}

    def test_previously_missing_stacks_present(self):
        for stack in ("distribution", "codebuild", "cowork-dashboard"):
            assert stack in DESTROYABLE_STACKS, f"{stack} must be destroyable"

    def test_no_duplicate_stacks(self):
        assert len(DESTROYABLE_STACKS) == len(set(DESTROYABLE_STACKS))


class TestDestroyReverseDependencyOrder:
    """Destroy runs in reverse dependency order relative to deploy."""

    def test_distribution_destroyed_before_networking(self):
        # distribution reads networking outputs, so it must be torn down first.
        assert DESTROYABLE_STACKS.index("distribution") < DESTROYABLE_STACKS.index("networking")

    def test_auth_destroyed_last(self):
        # auth is the root dependency; everything else goes first.
        assert DESTROYABLE_STACKS[-1] == "auth"

    def test_dependents_before_monitoring(self):
        # dashboard / cowork-dashboard / analytics / quota depend on monitoring.
        monitoring_idx = DESTROYABLE_STACKS.index("monitoring")
        for dependent in ("dashboard", "cowork-dashboard", "analytics", "quota"):
            assert DESTROYABLE_STACKS.index(dependent) < monitoring_idx


class TestLegacyStackOriginGuard:
    def test_matches_retired_gateway_signature(self):
        template = {
            "Parameters": {"GatewayImage": {}, "SpendEnforcementPosture": {}},
            "Resources": {"Database": {}, "LoadBalancer": {}},
        }

        assert _matches_legacy_stack_template("gateway", template) is True

    def test_rejects_upstream_cdk_stack_as_legacy_gateway(self):
        template = {
            "Parameters": {},
            "Resources": {"Gateway": {}, "Vpc": {}, "EcrRepository": {}},
        }

        assert _matches_legacy_stack_template("gateway", template) is False

    def test_matches_both_retired_bootstrap_variants(self):
        device_code = {
            "Parameters": {"OidcClientSecretArn": {}, "OidcTokenEndpoint": {}},
            "Resources": {"DeviceCodeTable": {}, "BootstrapHandlerFunction": {}},
        }
        bearer = {
            "Parameters": {"DefaultInferenceModels": {}, "OtlpIdentityMode": {}},
            "Resources": {"BootstrapFunction": {}, "BootstrapApi": {}},
        }

        assert _matches_legacy_stack_template("bootstrap", device_code) is True
        assert _matches_legacy_stack_template("bootstrap", bearer) is True

    def test_mismatch_refuses_before_pre_cleanup_or_delete(self):
        manager = Mock()
        manager.get_stack_status.return_value = "CREATE_COMPLETE"
        manager.cf_client.get_template.return_value = {"TemplateBody": {"Parameters": {}, "Resources": {"Gateway": {}}}}
        command = DestroyCommand()
        command._legacy_stack_type = "gateway"

        with patch("governed_inference_platform.cli.commands.destroy.CloudFormationManager", return_value=manager):
            result = command._delete_stack("ClaudeGatewayStack", "us-east-1", Console(file=StringIO()))

        assert result == 2
        manager.pre_cleanup_stack.assert_not_called()
        manager.delete_stack.assert_not_called()

    def test_get_template_failure_refuses_before_destructive_calls(self):
        manager = Mock()
        manager.get_stack_status.return_value = "CREATE_COMPLETE"
        manager.cf_client.get_template.side_effect = PermissionError("denied")
        command = DestroyCommand()
        command._legacy_stack_type = "gateway"

        with patch("governed_inference_platform.cli.commands.destroy.CloudFormationManager", return_value=manager):
            result = command._delete_stack("gip-gateway", "us-east-1", Console(file=StringIO()))

        assert result == 2
        manager.pre_cleanup_stack.assert_not_called()
        manager.delete_stack.assert_not_called()

    def test_matching_legacy_template_can_proceed(self):
        manager = Mock()
        manager.get_stack_status.return_value = "CREATE_COMPLETE"
        manager.cf_client.get_template.return_value = {
            "TemplateBody": {
                "Parameters": {"GatewayImage": {}, "SpendEnforcementPosture": {}},
                "Resources": {"Database": {}, "LoadBalancer": {}},
            }
        }
        manager.delete_stack.return_value = SimpleNamespace(success=True, error=None)
        command = DestroyCommand()
        command._legacy_stack_type = "gateway"

        with patch("governed_inference_platform.cli.commands.destroy.CloudFormationManager", return_value=manager):
            result = command._delete_stack("gip-gateway", "us-east-1", Console(file=StringIO()))

        assert result == 0
        manager.pre_cleanup_stack.assert_called_once()
        manager.delete_stack.assert_called_once()

    def test_explicit_gateway_cleanup_prints_snapshot_warning(self, capsys):
        profile = Profile(
            name="test",
            provider_domain="example.okta.com",
            client_id="client",
            credential_storage="session",
            aws_region="us-east-1",
            identity_pool_name="gip",
            sso_enabled=False,
        )
        with (
            patch("governed_inference_platform.cli.commands.destroy.Config") as config,
            patch.object(DestroyCommand, "_delete_stack", return_value=0),
            patch.object(DestroyCommand, "_get_retained_resources", return_value=[]),
            patch("governed_inference_platform.cli.commands.destroy.clear_cached_credentials", return_value=False),
        ):
            config.load.return_value.get_profile.return_value = profile
            config.load.return_value.active_profile = "test"
            result = CommandTester(DestroyCommand()).execute("gateway --force")

        assert result == 0
        output = capsys.readouterr().out
        assert "Legacy cleanup only" in output
        assert "snapshot-on-delete" in output
