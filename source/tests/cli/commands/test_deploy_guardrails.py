from pathlib import Path
from unittest.mock import MagicMock, patch

from cleo.testers.command_tester import CommandTester

from governed_inference_platform.cli.commands.deploy import (
    VALID_STACKS,
    DeployCommand,
    build_guardrails_params,
    guardrails_cleanup_regions,
    guardrails_name,
    guardrails_preflight,
    guardrails_regions,
)
from governed_inference_platform.cli.commands.destroy import DESTROYABLE_STACKS
from governed_inference_platform.config import Profile


def _profile(**overrides) -> Profile:
    data = {
        "name": "test",
        "provider_domain": "company.okta.com",
        "client_id": "0oa1example2",
        "credential_storage": "session",
        "aws_region": "us-east-1",
        "identity_pool_name": "gip-test",
        "provider_type": "okta",
        "allowed_bedrock_regions": ["us-east-1", "us-west-2"],
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
    def test_valid_stacks_includes_guardrails(self):
        assert "guardrails" in VALID_STACKS

    def test_destroyable_guardrails_before_auth(self):
        assert "guardrails" in DESTROYABLE_STACKS
        assert DESTROYABLE_STACKS.index("guardrails") < DESTROYABLE_STACKS.index("auth")

    def test_go_config_mirrors_guardrails_fields(self):
        config_go = Path(__file__).resolve().parents[4] / "source" / "go" / "internal" / "config" / "config.go"
        content = config_go.read_text(encoding="utf-8")

        assert 'json:"guardrails_enabled' in content
        assert 'json:"guardrails_name' in content
        assert 'json:"guardrails_content_filter_strength' in content
        assert 'json:"guardrails_model_include_list' in content
        assert 'json:"guardrails_kms_key_arn' in content


class TestParams:
    def test_guardrails_name_is_bedrock_safe_and_bounded(self):
        profile = _profile(identity_pool_name="team/path with spaces and a very very very very long suffix")
        name = guardrails_name(profile)

        assert len(name) <= 50
        assert name == "team-path-with-spaces-and-a-very-very-very-very-lo"

    def test_build_guardrails_params_defaults_to_all_models(self):
        params = build_guardrails_params(_profile())

        assert "GuardrailName=gip-test-guardrail" in params
        assert "ContentFilterStrength=MEDIUM" in params
        assert not any(p.startswith("ModelIncludeList=") for p in params)

    def test_build_guardrails_params_carries_optional_scope_and_kms(self):
        params = build_guardrails_params(
            _profile(
                guardrails_name="team-guardrail",
                guardrails_content_filter_strength="HIGH",
                guardrails_model_include_list=["us.anthropic.claude-sonnet-5", "anthropic.claude-haiku-4-5"],
                guardrails_kms_key_arn="arn:aws:kms:us-east-1:123456789012:key/00000000-1111-2222-3333-444444444444",
            )
        )

        assert "GuardrailName=team-guardrail" in params
        assert "ContentFilterStrength=HIGH" in params
        assert "ModelIncludeList=us.anthropic.claude-sonnet-5,anthropic.claude-haiku-4-5" in params
        assert any(p.startswith("KmsKeyArn=arn:aws:kms:") for p in params)

    def test_preflight_rejects_invalid_strength_and_model_list(self):
        ok, msg = guardrails_preflight(_profile(guardrails_content_filter_strength="NONE"))
        assert not ok and "LOW, MEDIUM, HIGH" in msg

        ok, msg = guardrails_preflight(_profile(guardrails_model_include_list=["valid", "bad,model"]))
        assert not ok and "without commas" in msg

    def test_preflight_rejects_multi_region_kms_key(self):
        ok, msg = guardrails_preflight(
            _profile(
                guardrails_kms_key_arn="arn:aws:kms:us-east-1:123456789012:key/00000000-1111-2222-3333-444444444444"
            )
        )

        assert not ok and "exactly one region" in msg

    def test_preflight_rejects_wrong_region_kms_key(self):
        ok, msg = guardrails_preflight(
            _profile(
                allowed_bedrock_regions=["us-west-2"],
                guardrails_kms_key_arn="arn:aws:kms:us-east-1:123456789012:key/00000000-1111-2222-3333-444444444444",
            )
        )

        assert not ok and "us-west-2" in msg

    def test_preflight_accepts_same_region_kms_key(self):
        ok, msg = guardrails_preflight(
            _profile(
                allowed_bedrock_regions=["us-east-1"],
                guardrails_kms_key_arn="arn:aws:kms:us-east-1:123456789012:key/00000000-1111-2222-3333-444444444444",
            )
        )

        assert ok and msg is None

    def test_regions_expand_sentinels_and_filter_partitions(self):
        regions = guardrails_regions(_profile(allowed_bedrock_regions=["all-commercial", "us-gov-west-1"]))

        assert "us-east-1" in regions
        assert "eu-west-1" in regions
        assert "us-gov-west-1" not in regions

    def test_empty_region_list_matches_auth_fallback(self):
        with patch(
            "governed_inference_platform.models.get_all_bedrock_regions",
            return_value=["us-east-1", "us-west-2", "us-gov-west-1"],
        ):
            regions = guardrails_regions(_profile(allowed_bedrock_regions=[]))

        assert regions == ["us-east-1", "us-west-2"]


class TestDispatch:
    def test_cleanup_regions_only_adds_explicit_abandoned_regions(self, monkeypatch):
        profile = _profile(allowed_bedrock_regions=["us-east-1"])
        monkeypatch.setenv("GIP_GUARDRAILS_CLEANUP_REGIONS", "ap-east-1,us-west-2")

        assert guardrails_cleanup_regions(profile) == ["us-east-1", "ap-east-1", "us-west-2"]

    def test_orphan_scan_only_probes_enabled_and_explicit_regions(self):
        profile = _profile(guardrails_enabled=False, allowed_bedrock_regions=["us-east-1", "ap-east-1"])
        cf_manager = MagicMock()
        cf_manager.get_stack_status.return_value = None
        cf_manager.session.client.return_value.describe_regions.return_value = {
            "Regions": [{"RegionName": "us-east-1"}, {"RegionName": "us-west-2"}]
        }

        with patch("governed_inference_platform.cli.commands.deploy.CloudFormationManager") as manager_cls:
            manager_cls.return_value.get_stack_status.return_value = None
            DeployCommand()._check_orphaned_stacks([], profile, cf_manager, None)

        assert {call.kwargs["region"] for call in manager_cls.call_args_list} == {"us-west-2"}

    def test_orphan_scan_falls_back_to_primary_region_when_discovery_fails(self):
        profile = _profile(guardrails_enabled=False, allowed_bedrock_regions=["us-east-1", "ap-east-1"])
        cf_manager = MagicMock()
        cf_manager.get_stack_status.return_value = None
        cf_manager.session.client.return_value.describe_regions.side_effect = RuntimeError("denied")

        with patch("governed_inference_platform.cli.commands.deploy.CloudFormationManager") as manager_cls:
            DeployCommand()._check_orphaned_stacks([], profile, cf_manager, None)

        manager_cls.assert_not_called()

    def test_deploy_guardrails_requires_opt_in(self, capsys):
        rc = _run_deploy(_profile(guardrails_enabled=False), "guardrails")
        output = capsys.readouterr().out

        assert rc == 1
        assert "Guardrails enforcement is not enabled" in output

    def test_deploy_guardrails_dry_run_schedules_stack(self, capsys):
        rc = _run_deploy(_profile(guardrails_enabled=True), "guardrails --dry-run")
        output = capsys.readouterr().out

        assert rc == 0
        assert "Unknown stack" not in output
        assert "guardrails" in output

    def test_show_commands_prints_each_guardrails_region(self, capsys):
        rc = _run_deploy(_profile(guardrails_enabled=True), "guardrails --show-commands")
        output = capsys.readouterr().out

        assert rc == 0
        assert "guardrails-enforcement.yaml" in output
        assert "--region us-east-1" in output
        assert "--region us-west-2" in output

    def test_explicit_guardrails_failure_returns_nonzero(self, capsys):
        profile = _profile(guardrails_enabled=True)
        with (
            patch("governed_inference_platform.cli.commands.deploy.Config") as MockConfig,
            patch("governed_inference_platform.cli.commands.deploy.CloudFormationManager") as MockCFM,
            patch.object(DeployCommand, "_deploy_stack", return_value=1),
        ):
            MockConfig.load.return_value.get_profile.return_value = profile
            MockConfig.load.return_value.active_profile = "test"
            MockCFM.return_value.get_stack_status.return_value = None
            rc = CommandTester(DeployCommand()).execute("guardrails")

        assert rc == 1
        assert "Failed to deploy guardrails stack" in capsys.readouterr().out

    def test_orphan_check_includes_disabled_guardrails_stack(self):
        profile = _profile(guardrails_enabled=False)
        cf_manager = MagicMock()
        cf_manager.get_stack_status.side_effect = lambda name: (
            "CREATE_COMPLETE" if name.endswith("guardrails") else None
        )
        cf_manager.session.client.return_value.describe_regions.return_value = {
            "Regions": [{"RegionName": profile.aws_region}]
        }

        with patch(
            "governed_inference_platform.cli.commands.deploy.guardrails_cleanup_regions",
            return_value=[profile.aws_region],
        ):
            orphaned = DeployCommand()._check_orphaned_stacks([], profile, cf_manager, None)

        assert any(orphan[0] == "guardrails" for orphan in orphaned)

    def test_orphan_check_finds_guardrails_abandoned_by_region_change(self):
        profile = _profile(guardrails_enabled=True, allowed_bedrock_regions=["us-east-1"])
        cf_manager = MagicMock()
        cf_manager.get_stack_status.return_value = None
        cf_manager.session.client.return_value.describe_regions.return_value = {
            "Regions": [{"RegionName": "us-east-1"}, {"RegionName": "us-west-2"}]
        }
        stale_region_manager = MagicMock()
        stale_region_manager.get_stack_status.return_value = "CREATE_COMPLETE"

        with (
            patch(
                "governed_inference_platform.cli.commands.deploy.guardrails_cleanup_regions",
                return_value=["us-east-1", "us-west-2"],
            ),
            patch(
                "governed_inference_platform.cli.commands.deploy.CloudFormationManager",
                return_value=stale_region_manager,
            ),
        ):
            orphaned = DeployCommand()._check_orphaned_stacks(
                [("guardrails", "Bedrock Guardrails account-level enforcement")],
                profile,
                cf_manager,
                None,
            )

        assert any(orphan[0] == "guardrails" for orphan in orphaned)
        assert any(orphan[0] == "guardrails" and orphan[3] == "us-west-2" for orphan in orphaned)

    def test_orphan_check_skips_inaccessible_guardrails_cleanup_region(self):
        profile = _profile(guardrails_enabled=True, allowed_bedrock_regions=["us-east-1"])
        cf_manager = MagicMock()
        cf_manager.get_stack_status.return_value = None
        cf_manager.session.client.return_value.describe_regions.return_value = {
            "Regions": [{"RegionName": "us-east-1"}, {"RegionName": "us-west-2"}]
        }
        inaccessible_region_manager = MagicMock()
        inaccessible_region_manager.get_stack_status.side_effect = RuntimeError("region denied")

        with (
            patch(
                "governed_inference_platform.cli.commands.deploy.guardrails_cleanup_regions",
                return_value=["us-east-1", "us-west-2"],
            ),
            patch(
                "governed_inference_platform.cli.commands.deploy.CloudFormationManager",
                return_value=inaccessible_region_manager,
            ),
        ):
            orphaned = DeployCommand()._check_orphaned_stacks(
                [("guardrails", "Bedrock Guardrails account-level enforcement")],
                profile,
                cf_manager,
                None,
            )

        assert not orphaned


def test_no_iam_guardrail_identifier_condition_in_auth_templates():
    infra = Path(__file__).parent.parent.parent.parent.parent / "deployment" / "infrastructure"
    auth_templates = list(infra.glob("bedrock-auth-*.yaml")) + [infra / "cognito-identity-pool.yaml"]

    assert auth_templates
    for template in auth_templates:
        assert "bedrock:GuardrailIdentifier" not in template.read_text(encoding="utf-8")
