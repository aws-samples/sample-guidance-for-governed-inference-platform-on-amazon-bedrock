"""Tests for deploying the oversized analytics template."""

from unittest.mock import MagicMock, patch

from rich.console import Console

from governed_inference_platform.cli.commands.deploy import DeployCommand
from governed_inference_platform.cli.utils.cloudformation import StackDeploymentResult


def test_analytics_deploy_passes_existing_artifacts_bucket():
    profile = MagicMock()
    profile.aws_region = "us-east-1"
    profile.identity_pool_name = "gip-test"
    profile.stack_names = {"analytics": "gip-test-analytics", "s3": "gip-test-s3bucket"}
    profile.metrics_log_group = "/aws/gip/metrics"
    profile.data_retention_days = 30
    profile.firehose_buffer_interval = 60
    profile.analytics_debug_mode = False
    profile.tags = {}
    manager = MagicMock(region="us-east-1")
    manager.deploy_stack.return_value = StackDeploymentResult(success=True)

    with patch(
        "governed_inference_platform.cli.commands.deploy.get_stack_outputs",
        return_value={"CfnArtifactsBucket": "existing-artifacts"},
    ):
        result = DeployCommand()._deploy_stack("analytics", profile, Console(), manager)

    assert result == 0
    assert manager.deploy_stack.call_args.kwargs["artifacts_bucket"] == "existing-artifacts"
