# ABOUTME: Regression tests for `gip deploy s3bucket` dispatch (review finding C-2)
# ABOUTME: Pins that every stack the CLI's own error messages recommend is actually dispatchable

"""Regression tests for the `gip deploy s3bucket` recovery path.

Before the fix, deploy.py's quota and regional-artifacts error paths told
users to `Run: gip deploy s3bucket`, but "s3bucket" was missing from
VALID_STACKS and the explicit-arg dispatch chain — the command answered
"Unknown stack: s3bucket". Only a full `gip deploy` could recover.
"""

import re
import sys
from pathlib import Path
from unittest.mock import patch

from cleo.testers.command_tester import CommandTester

# ruff: noqa: E402
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from governed_inference_platform.cli.commands.deploy import VALID_STACKS, DeployCommand
from governed_inference_platform.cli.commands.destroy import DESTROYABLE_STACKS
from governed_inference_platform.config import Profile

DEPLOY_SOURCE = (
    Path(__file__).resolve().parents[3] / "governed_inference_platform" / "cli" / "commands" / "deploy.py"
).read_text(encoding="utf-8")


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


class TestS3BucketRegistration:
    def test_valid_stacks_includes_s3bucket(self):
        assert "s3bucket" in VALID_STACKS

    def test_destroyable_stacks_includes_s3bucket(self):
        # Destroy side was already wired; keep the pair pinned together.
        assert "s3bucket" in DESTROYABLE_STACKS


class TestS3BucketDispatch:
    def test_deploy_s3bucket_is_dispatchable(self, capsys):
        """`gip deploy s3bucket --dry-run` schedules the stack instead of 'Unknown stack'."""
        rc = _run_deploy(_profile(), "s3bucket --dry-run")
        output = capsys.readouterr().out
        assert rc == 0
        assert "Unknown stack" not in output
        assert any(line.strip().startswith("s3bucket") for line in output.splitlines())

    def test_unknown_stack_still_rejected(self, capsys):
        rc = _run_deploy(_profile(), "not-a-stack --dry-run")
        output = capsys.readouterr().out
        assert rc == 1
        assert "Unknown stack" in output


class TestRecoveryMessagesAreDispatchable:
    """Every stack deploy.py tells the user to `gip deploy <stack>` must dispatch."""

    def test_all_recommended_deploy_commands_are_valid_stacks(self):
        recommended = set(re.findall(r"gip deploy ([a-z0-9-]+)", DEPLOY_SOURCE))
        recommended.discard("command")  # prose, not a stack name
        unreachable = {s for s in recommended if s not in VALID_STACKS}
        assert not unreachable, f"error messages recommend non-dispatchable stacks: {sorted(unreachable)}"

    def test_s3bucket_is_recommended_by_error_paths(self):
        # Guard: the recovery messages this fix unbreaks still exist.
        assert "gip deploy s3bucket" in DEPLOY_SOURCE


class TestValidStacksDestroyParity:
    def test_every_valid_stack_is_destroyable(self):
        missing = set(VALID_STACKS) - set(DESTROYABLE_STACKS)
        assert not missing, f"deployable-by-arg stacks missing from destroy: {sorted(missing)}"
