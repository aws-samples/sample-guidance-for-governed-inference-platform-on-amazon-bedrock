# ABOUTME: Tests for `gip deploy model-lifecycle` dispatch, tracked-model seeding, and region selection
# ABOUTME: Covers VALID/DESTROYABLE registration, opt-in gating, compute_tracked_models, lifecycle_check_regions

"""Deploy-side tests for the model-lifecycle stack (E-S2 / R14 §2.3).

The stack is opt-in (`model_lifecycle_enabled`), leaf-level (destroy before
quota, whose SNS topic it may reference), and seeded at deploy time: the
tracked-models SSM parameter gets the profile's selected model + resolved
tier defaults + extra_models overlay entries.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from cleo.testers.command_tester import CommandTester
from rich.console import Console

# ruff: noqa: E402
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from governed_inference_platform.cli.commands.deploy import (
    VALID_STACKS,
    DeployCommand,
    compute_tracked_models,
    lifecycle_check_regions,
)
from governed_inference_platform.cli.commands.destroy import DESTROYABLE_STACKS
from governed_inference_platform.cli.utils.cloudformation import StackDeploymentResult
from governed_inference_platform.config import Profile

TEMPLATE_PATH = Path(__file__).parents[4] / "deployment" / "infrastructure" / "model-lifecycle.yaml"


def _profile(**overrides) -> Profile:
    data = {
        "name": "test",
        "provider_domain": "company.okta.com",
        "client_id": "0oa1example2",
        "credential_storage": "session",
        "aws_region": "us-east-1",
        "identity_pool_name": "gip",
        "provider_type": "okta",
    }
    data.update(overrides)
    return Profile.from_dict(data)


def _run_deploy(profile, args):
    with (
        patch("governed_inference_platform.cli.commands.deploy.Config") as MockConfig,
        patch("governed_inference_platform.cli.commands.deploy.CloudFormationManager") as MockCFM,
    ):
        MockConfig.load.return_value.get_profile.return_value = profile
        MockConfig.load.return_value.active_profile = "test"
        MockCFM.return_value.get_stack_status.return_value = None
        tester = CommandTester(DeployCommand())
        return tester.execute(args)


class TestRegistration:
    def test_valid_stacks_includes_model_lifecycle(self):
        assert "model-lifecycle" in VALID_STACKS

    def test_destroyable_before_quota(self):
        """The stack may reference quota's SNS topic — it must be destroyed first."""
        assert "model-lifecycle" in DESTROYABLE_STACKS
        assert DESTROYABLE_STACKS.index("model-lifecycle") < DESTROYABLE_STACKS.index("quota")


class TestDispatch:
    def test_deploy_model_lifecycle_requires_opt_in(self, capsys):
        rc = _run_deploy(_profile(model_lifecycle_enabled=False), "model-lifecycle")
        output = capsys.readouterr().out
        assert rc == 1
        assert "not enabled" in output
        assert "gip init" in output

    def test_deploy_model_lifecycle_dry_run_schedules_stack(self, capsys):
        rc = _run_deploy(_profile(model_lifecycle_enabled=True), "model-lifecycle --dry-run")
        output = capsys.readouterr().out
        assert rc == 0
        assert "Unknown stack" not in output
        assert "model-lifecycle" in output

    def test_show_commands_prints_package_and_seed_steps(self, capsys):
        """--show-commands must print the cloudformation package step AND the SSM seed step."""
        rc = _run_deploy(
            _profile(
                model_lifecycle_enabled=True,
                selected_model="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
                cross_region_profile="us",
            ),
            "model-lifecycle --show-commands",
        )
        output = capsys.readouterr().out
        assert rc == 0
        assert "model-lifecycle.yaml" in output
        assert "aws ssm put-parameter" in output
        assert "/gip/test/tracked-models" in output
        assert "us.anthropic.claude-sonnet-4-5-20250929-v1:0" in output

    @pytest.mark.parametrize(
        ("stack_arg", "overrides"),
        [
            ("memory", {"web_search_enabled": True, "memory_enabled": True, "memory_user_enabled": True}),
            ("skills", {"skills_registry_enabled": True}),
            ("model-lifecycle", {"model_lifecycle_enabled": True}),
        ],
    )
    def test_explicit_optional_stack_failure_returns_nonzero(self, stack_arg, overrides, capsys):
        profile = _profile(**overrides)
        with (
            patch("governed_inference_platform.cli.commands.deploy.Config") as MockConfig,
            patch("governed_inference_platform.cli.commands.deploy.CloudFormationManager") as MockCFM,
            patch.object(DeployCommand, "_deploy_stack", return_value=1),
        ):
            MockConfig.load.return_value.get_profile.return_value = profile
            MockConfig.load.return_value.active_profile = "test"
            MockCFM.return_value.get_stack_status.return_value = None
            rc = CommandTester(DeployCommand()).execute(stack_arg)

        assert rc == 1
        assert f"Failed to deploy {stack_arg} stack" in capsys.readouterr().out


QUOTA_TOPIC_ARN = "arn:aws:sns:us-east-1:123456789012:gip-quota-alerts"
QUOTA_KEY_ARN = "arn:aws:kms:us-east-1:123456789012:key/1234abcd-12ab-34cd-56ef-1234567890ab"


def _deploy_lifecycle_stack(profile, quota_outputs) -> dict[str, str]:
    """Run the model-lifecycle branch of _deploy_stack with AWS I/O mocked.

    Returns the CloudFormation parameters handed to deploy_stack as a
    {key: value} map. ``quota_outputs`` is what get_stack_outputs returns for
    the quota stack (None = not deployed).
    """

    def outputs(stack_name, region):
        if stack_name.endswith("-s3bucket"):
            return {"CfnArtifactsBucket": "gip-artifacts"}
        if stack_name.endswith("-quota"):
            return quota_outputs
        return {"LifecycleAlertTopicArn": QUOTA_TOPIC_ARN}

    manager = MagicMock(region="us-east-1")
    manager.deploy_stack.return_value = StackDeploymentResult(success=True)
    with (
        patch("governed_inference_platform.cli.commands.deploy.get_stack_outputs", side_effect=outputs),
        patch("governed_inference_platform.cli.commands.deploy.run_checked", return_value=MagicMock(returncode=0)),
        patch("boto3.client"),
    ):
        rc = DeployCommand()._deploy_stack("model-lifecycle", profile, Console(), manager)
    assert rc == 0
    return {p["ParameterKey"]: p["ParameterValue"] for p in manager.deploy_stack.call_args.kwargs["parameters"]}


class TestQuotaTopicReuse:
    """gip deploy reuses the quota stack's alert topic (R14 §2.3). That topic is
    KMS-encrypted (security review), so the check Lambda can only publish if the
    template also receives the key ARN (AlertTopicKmsKeyArn -> a kms grant on
    LifecycleCheckRole). These pin the CLI side of that contract."""

    def _profile(self):
        return _profile(
            model_lifecycle_enabled=True,
            quota_monitoring_enabled=True,
            selected_model="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            cross_region_profile="us",
        )

    def test_reused_encrypted_quota_topic_passes_its_key_arn(self):
        params = _deploy_lifecycle_stack(
            self._profile(),
            {"QuotaAlertTopicArn": QUOTA_TOPIC_ARN, "QuotaAlertTopicKmsKeyArn": QUOTA_KEY_ARN},
        )
        assert params["AlertTopicArn"] == QUOTA_TOPIC_ARN
        assert params["AlertTopicKmsKeyArn"] == QUOTA_KEY_ARN

    def test_reused_unencrypted_quota_topic_passes_no_key_arn(self):
        """The quota stack itself reused an external, unencrypted topic (no
        QuotaAlertTopicKmsKeyArn output): reuse must keep working, key-less."""
        params = _deploy_lifecycle_stack(self._profile(), {"QuotaAlertTopicArn": QUOTA_TOPIC_ARN})
        assert params["AlertTopicArn"] == QUOTA_TOPIC_ARN
        assert "AlertTopicKmsKeyArn" not in params

    def test_without_quota_stack_the_template_creates_its_own_topic(self):
        """No quota stack (or quota monitoring off): neither parameter is passed,
        so the template's CreateTopic path (own topic + own key) applies."""
        for profile, quota_outputs in (
            (self._profile(), None),
            (_profile(model_lifecycle_enabled=True, quota_monitoring_enabled=False, cross_region_profile="us"), None),
        ):
            params = _deploy_lifecycle_stack(profile, quota_outputs)
            assert "AlertTopicArn" not in params
            assert "AlertTopicKmsKeyArn" not in params
            assert set(params) == {"ProfileName", "CheckRegions"}

    def test_passed_parameters_exist_in_the_template(self):
        """Every parameter deploy.py can pass is declared by model-lifecycle.yaml."""

        class _Loader(yaml.SafeLoader):
            pass

        for tag in ("!Ref", "!Sub", "!GetAtt", "!If", "!Join", "!Equals", "!Not", "!And", "!Or", "!Condition"):
            _Loader.add_constructor(tag, lambda loader, node: None)
        declared = set(yaml.load(TEMPLATE_PATH.read_text(encoding="utf-8"), Loader=_Loader)["Parameters"])  # nosec B506
        params = _deploy_lifecycle_stack(
            self._profile(),
            {"QuotaAlertTopicArn": QUOTA_TOPIC_ARN, "QuotaAlertTopicKmsKeyArn": QUOTA_KEY_ARN},
        )
        assert set(params) <= declared, set(params) - declared


class TestComputeTrackedModels:
    def test_selected_model_and_tier_defaults(self):
        profile = _profile(
            selected_model="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            cross_region_profile="us",
        )
        tracked = compute_tracked_models(profile)
        assert tracked[0] == "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
        # Resolved tier defaults are appended (deduplicated), all in the us geography.
        assert len(tracked) == len(set(tracked))
        assert all(t.startswith("us.") for t in tracked)

    def test_overlay_models_are_tracked(self):
        profile = _profile(
            selected_model="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            cross_region_profile="us",
            extra_models={
                "example-9": {
                    "name": "Claude Example 9",
                    "base_model_id": "anthropic.claude-example-9-v1:0",
                    "profiles": {
                        "us": {
                            "model_id": "us.anthropic.claude-example-9-v1:0",
                            "source_regions": ["us-east-1"],
                            "destination_regions": ["us-east-1"],
                        }
                    },
                }
            },
        )
        assert "us.anthropic.claude-example-9-v1:0" in compute_tracked_models(profile)

    def test_invalid_overlay_does_not_block_deploy(self):
        """A bad hand-edited overlay is surfaced by init/package — deploy still tracks the rest."""
        profile = _profile(
            selected_model="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            cross_region_profile="us",
            extra_models={"sonnet-4-5": {"name": "override attempt"}},
        )
        tracked = compute_tracked_models(profile)
        assert tracked[0] == "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

    def test_no_selected_model_still_tracks_tier_defaults(self):
        tracked = compute_tracked_models(_profile(cross_region_profile="us"))
        assert tracked, "tier defaults alone must produce a non-empty tracked list"


class TestLifecycleCheckRegions:
    def test_uses_allowed_bedrock_regions(self):
        profile = _profile(allowed_bedrock_regions=["us-east-1", "us-west-2"])
        assert lifecycle_check_regions(profile) == ["us-east-1", "us-west-2"]

    def test_falls_back_to_profile_region(self):
        assert lifecycle_check_regions(_profile(allowed_bedrock_regions=[])) == ["us-east-1"]

    def test_govcloud_regions_filtered_from_commercial_deployment(self):
        """GovCloud lifecycle is only visible from GovCloud (R14 §1.2) — don't poll it from commercial."""
        profile = _profile(allowed_bedrock_regions=["us-east-1", "us-gov-west-1"])
        assert lifecycle_check_regions(profile) == ["us-east-1"]

    def test_commercial_regions_filtered_from_govcloud_deployment(self):
        profile = _profile(aws_region="us-gov-west-1", allowed_bedrock_regions=["us-east-1", "us-gov-west-1"])
        assert lifecycle_check_regions(profile) == ["us-gov-west-1"]

    def test_sentinels_expanded(self):
        profile = _profile(allowed_bedrock_regions=["all-commercial"])
        regions = lifecycle_check_regions(profile)
        assert "us-east-1" in regions and "eu-west-1" in regions
        assert not any(r.startswith("us-gov") for r in regions)
