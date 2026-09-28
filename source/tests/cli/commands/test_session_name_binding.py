# ABOUTME: Round-trip tests for the opt-in SessionNameBinding parameter (ADR-0015)
# ABOUTME: Covers Profile defaults, deploy/package pass-through contracts, and answers-file validation

"""Session-name binding plumbing tests (REVIEW.md finding #2 / R7).

The auth templates' SessionNameBinding parameter must flow: init (wizard or
answers file) -> Profile.session_name_binding -> deploy (stack parameter) ->
package (client config.json) -> Go credential-process. These tests pin the
Python side of that chain; template structure is pinned by
test_auth_template_parity.py and the Go side by sts_test.go.
"""

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from governed_inference_platform.cli.commands.init import InitCommand
from governed_inference_platform.cli.commands.init_answers import build_config_from_answers
from governed_inference_platform.cli.commands.package import PackageCommand
from governed_inference_platform.config import Config, Profile

SOURCE_DIR = Path(__file__).parent.parent.parent.parent / "governed_inference_platform"
DEPLOY_PY = SOURCE_DIR / "cli" / "commands" / "deploy.py"
PACKAGE_PY = SOURCE_DIR / "cli" / "commands" / "package.py"

MINIMAL_ANSWERS = {"okta": {"domain": "company.okta.com", "client_id": "0oa0000000000000000"}}


class TestProfileField:
    """Profile carries the field with a backwards-compatible default."""

    def test_default_is_none_mode(self):
        profile = Profile(
            name="t",
            provider_domain="company.okta.com",
            client_id="cid",
            credential_storage="session",
            aws_region="us-east-1",
            identity_pool_name="gip-test",
        )
        assert profile.session_name_binding == "none"

    def test_old_profile_without_field_loads_as_none(self):
        """Profiles saved before ADR-0015 must load unchanged (backwards compat)."""
        old = {
            "name": "legacy",
            "provider_domain": "company.okta.com",
            "client_id": "cid",
            "credential_storage": "session",
            "aws_region": "us-east-1",
            "identity_pool_name": "gip-test",
        }
        profile = Profile.from_dict(old)
        assert profile.session_name_binding == "none"

    def test_round_trips_through_dict(self):
        profile = Profile(
            name="t",
            provider_domain="company.okta.com",
            client_id="cid",
            credential_storage="session",
            aws_region="us-east-1",
            identity_pool_name="gip-test",
            session_name_binding="email",
        )
        assert Profile.from_dict(profile.to_dict()).session_name_binding == "email"


class TestDeployPassThrough:
    """Both auth-stack parameter builders must pass SessionNameBinding."""

    def test_both_auth_param_paths_send_the_parameter(self):
        src = DEPLOY_PY.read_text(encoding="utf-8")
        occurrences = re.findall(r"SessionNameBinding=", src)
        assert len(occurrences) >= 2, (
            "deploy.py must append SessionNameBinding= in both auth param builders "
            f"(deploy and print-command paths), found {len(occurrences)}"
        )

    def test_deploy_guards_auth0_sub(self):
        """Auth0 templates reject 'sub' (AllowedValues) — deploy must fail early with guidance."""
        src = DEPLOY_PY.read_text(encoding="utf-8")
        assert re.search(r'provider_type == "auth0" and session_binding == "sub"', src), (
            "deploy.py must guard the unsupported auth0+sub combination before CloudFormation"
        )

    def test_package_emits_field_only_when_bound(self, tmp_path):
        profile = Profile(
            name="bound",
            provider_domain="company.okta.com",
            client_id="cid",
            credential_storage="session",
            aws_region="us-east-1",
            identity_pool_name="gip-test",
            session_name_binding="email",
        )
        config_path = PackageCommand()._create_config(
            tmp_path, profile, "arn:aws:iam::123456789012:role/gip", federation_type="direct"
        )

        saved = json.loads(config_path.read_text(encoding="utf-8"))["profiles"]["gip"]
        assert saved["session_name_binding"] == "email"

    def test_package_omits_none_binding(self, tmp_path):
        profile = Profile(
            name="unbound",
            provider_domain="company.okta.com",
            client_id="cid",
            credential_storage="session",
            aws_region="us-east-1",
            identity_pool_name="gip-test",
            session_name_binding="none",
        )

        config_path = PackageCommand()._create_config(
            tmp_path, profile, "arn:aws:iam::123456789012:role/gip", federation_type="direct"
        )

        saved = json.loads(config_path.read_text(encoding="utf-8"))["profiles"]["gip"]
        assert "session_name_binding" not in saved


class TestAnswersFileValidation:
    """init_answers accepts the new key with wizard-equivalent validation."""

    def test_default_is_none_mode(self):
        config, errors = build_config_from_answers(MINIMAL_ANSWERS)
        assert errors == []
        assert config["session_name_binding"] == "none"

    @pytest.mark.parametrize("value", ["email", "sub"])
    def test_valid_modes_accepted(self, value):
        answers = {**MINIMAL_ANSWERS, "session_name_binding": value}
        config, errors = build_config_from_answers(answers)
        assert errors == []
        assert config["session_name_binding"] == value

    def test_invalid_mode_rejected(self):
        answers = {**MINIMAL_ANSWERS, "session_name_binding": "e-mail"}
        _, errors = build_config_from_answers(answers)
        assert any("session_name_binding" in e for e in errors)

    def test_auth0_sub_rejected(self):
        answers = {
            "okta": {"domain": "company.auth0.com", "client_id": "abc123def" + "4567890"},
            "session_name_binding": "sub",
        }
        _, errors = build_config_from_answers(answers)
        assert any("session_name_binding" in e and "Auth0" in e for e in errors), (
            f"auth0 + sub must be rejected (Auth0 sub contains '|'), got errors: {errors}"
        )

    def test_auth0_email_accepted(self):
        answers = {
            "okta": {"domain": "company.auth0.com", "client_id": "abc123def" + "4567890"},
            "session_name_binding": "email",
        }
        config, errors = build_config_from_answers(answers)
        assert errors == []
        assert config["session_name_binding"] == "email"


class TestFromFileRoundTrip:
    """--from-file writes the field into the saved profile."""

    @pytest.fixture
    def config_paths(self, tmp_path):
        config_dir = tmp_path / ".gip"
        config_dir.mkdir()
        profiles_dir = config_dir / "profiles"
        profiles_dir.mkdir()
        config_file = config_dir / "config.json"
        config_file.write_text(json.dumps({"schema_version": "2.0", "active_profile": None}))
        with (
            patch.object(Config, "CONFIG_DIR", config_dir),
            patch.object(Config, "CONFIG_FILE", config_file),
            patch.object(Config, "PROFILES_DIR", profiles_dir),
            patch.object(InitCommand, "_stack_exists", side_effect=Exception("no creds")),
        ):
            yield profiles_dir

    def test_from_file_persists_binding(self, config_paths, tmp_path):
        from tests.support.cli import CliTester

        answers = tmp_path / "answers.json"
        answers.write_text(json.dumps({**MINIMAL_ANSWERS, "session_name_binding": "email"}))
        tester = CliTester(InitCommand())

        rc = tester.run(f"--from-file {answers} --profile-name bound")

        assert rc == 0
        saved = json.loads((config_paths / "bound.json").read_text())
        assert saved["session_name_binding"] == "email"

    def test_from_file_default_persists_none(self, config_paths, tmp_path):
        from tests.support.cli import CliTester

        answers = tmp_path / "answers.json"
        answers.write_text(json.dumps(MINIMAL_ANSWERS))
        tester = CliTester(InitCommand())

        rc = tester.run(f"--from-file {answers} --profile-name unbound")

        assert rc == 0
        saved = json.loads((config_paths / "unbound.json").read_text())
        assert saved["session_name_binding"] == "none"
