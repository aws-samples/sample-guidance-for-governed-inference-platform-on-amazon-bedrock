# ABOUTME: Tests for `gip init --from-file` (non-interactive/GitOps) and --export-answers
# ABOUTME: Covers defaults merge, validation error aggregation, unknown keys, round-trip, and the no-prompt guarantee

"""Tests for the non-interactive `gip init --from-file` / `--export-answers` mode.

The answers file mirrors the wizard's internal config dict. These tests verify:
- a minimal valid YAML creates a profile with wizard-default values,
- missing required fields fail with ALL problems listed (exit 1),
- unknown keys are rejected (typo protection),
- export -> import round-trips to an equivalent profile,
- no questionary prompt ever fires with --from-file (CI has no TTY),
- raw secrets in the file are rejected,
- profile collisions require --force.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.support.cli import CliTester

# ruff: noqa: E402
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from governed_inference_platform.cli.commands.init import InitCommand
from governed_inference_platform.config import Config

MINIMAL_YAML = """
okta:
  domain: company.okta.com
  client_id: 0oa0000000000000000
"""

OKTA_COST_YAML = """
okta:
  domain: acme.okta.com
  client_id: 0oa0000000000000001
aws:
  region: us-west-2
  identity_pool_name: acme-claude
quota:
  limit_type: cost
  monthly_cost_limit: 75.0
  daily_cost_limit: 5.0
  monthly_enforcement_mode: block
metering:
  enabled: true
  mode: max
tags:
  team: platform
  cost-center: eng-42
"""


@pytest.fixture
def config_paths(tmp_path):
    """Patch Config storage paths into tmp_path and stub out AWS stack checks."""
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
        # Never reach out to AWS during export's _check_existing_deployment reuse
        patch.object(InitCommand, "_stack_exists", side_effect=Exception("no creds")),
    ):
        yield profiles_dir


@pytest.fixture
def tester():
    return CliTester(InitCommand())


def _load_profile(profiles_dir: Path, name: str) -> dict:
    return json.loads((profiles_dir / f"{name}.json").read_text())


class TestFromFileMinimal:
    def test_minimal_yaml_creates_profile_with_wizard_defaults(self, config_paths, tester, tmp_path):
        """A minimal answers file (only the two no-default fields) creates a full profile."""
        answers = tmp_path / "answers.yaml"
        answers.write_text(MINIMAL_YAML)

        rc = tester.run(f"--from-file {answers} --profile-name ci")

        assert rc == 0
        saved = _load_profile(config_paths, "ci")
        # Required values from the file
        assert saved["provider_domain"] == "company.okta.com"
        assert saved["client_id"] == "0oa0000000000000000"
        # Wizard press-Enter defaults for a minimal OIDC deployment
        assert saved["auth_type"] == "oidc"
        assert saved["sso_enabled"] is True
        assert saved["provider_type"] == "okta"  # auto-detected from domain
        assert saved["credential_storage"] == "session"
        assert saved["federation_type"] == "direct"
        assert saved["max_session_duration"] == 43200
        assert saved["aws_region"] == "us-east-1"
        assert saved["identity_pool_name"] == "gip-auth"
        assert saved["stack_names"]["auth"] == "gip-auth-stack"
        assert saved["monitoring_enabled"] is True
        assert saved["monitoring_mode"] == "sidecar"
        assert saved["analytics_enabled"] is False  # sidecar mode has no analytics pipeline
        assert saved["selected_model"] == "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
        assert saved["cross_region_profile"] == "us"
        assert saved["selected_source_region"] == "us-east-1"
        assert saved["allowed_bedrock_regions"]  # derived from model + profile
        assert saved["quota_monitoring_enabled"] is True
        assert saved["quota_limit_type"] == "cost"
        assert saved["monthly_cost_limit"] == 50.0
        assert saved["monthly_token_limit"] == 0  # cost mode zeroes token limits
        assert saved["quota_check_interval"] == 30
        assert saved["settings_target"] == "user"
        assert saved["enable_codebuild"] is False
        assert saved["web_search_enabled"] is False
        assert saved["cowork_3p_enabled"] is True
        assert saved["enable_distribution"] is False

    def test_json_answers_file_supported(self, config_paths, tester, tmp_path):
        answers = tmp_path / "answers.json"
        answers.write_text(json.dumps({"okta": {"domain": "company.okta.com", "client_id": "0oa0000000000000000"}}))

        rc = tester.run(f"--from-file {answers} --profile-name ci-json")

        assert rc == 0
        assert (config_paths / "ci-json.json").exists()

    def test_explicit_values_override_defaults(self, config_paths, tester, tmp_path):
        answers = tmp_path / "answers.yaml"
        answers.write_text(OKTA_COST_YAML)

        rc = tester.run(f"--from-file {answers} --profile-name acme")

        assert rc == 0
        saved = _load_profile(config_paths, "acme")
        assert saved["aws_region"] == "us-west-2"
        assert saved["identity_pool_name"] == "acme-claude"
        assert saved["stack_names"]["monitoring"] == "acme-claude-monitoring"
        assert saved["monthly_cost_limit"] == 75.0
        assert saved["daily_cost_limit"] == 5.0
        assert saved["metering_enabled"] is True
        assert saved["metering_mode"] == "max"
        assert saved["tags"] == {"team": "platform", "cost-center": "eng-42"}


class TestFromFileValidation:
    def test_missing_required_fields_lists_all_problems(self, config_paths, tester, tmp_path, capsys):
        """An empty file must fail listing BOTH no-default fields, not just the first."""
        answers = tmp_path / "answers.yaml"
        answers.write_text("{}\n")

        rc = tester.run(f"--from-file {answers} --profile-name ci")

        assert rc == 1
        out = capsys.readouterr().out
        assert "okta.domain" in out
        assert "okta.client_id" in out
        assert not (config_paths / "ci.json").exists()

    def test_multiple_invalid_fields_all_reported(self, config_paths, tester, tmp_path, capsys):
        answers = tmp_path / "answers.yaml"
        answers.write_text(
            """
okta:
  domain: company.okta.com
  client_id: 0oa0000000000000000
credential_storage: floppy-disk
federation_type: carrier-pigeon
max_session_duration: 99
quota:
  burst_buffer_percent: 99
"""
        )

        rc = tester.run(f"--from-file {answers} --profile-name ci")

        assert rc == 1
        out = capsys.readouterr().out
        assert "credential_storage" in out
        assert "federation_type" in out
        assert "max_session_duration" in out
        assert "burst_buffer_percent" in out

    def test_unknown_key_rejected(self, config_paths, tester, tmp_path, capsys):
        """Typos must hard-error, listing the offending keys."""
        answers = tmp_path / "answers.yaml"
        answers.write_text(
            """
okta:
  domain: company.okta.com
  client_id: 0oa0000000000000000
monitring:
  enabled: true
quota:
  monthly_limt: 5
"""
        )

        rc = tester.run(f"--from-file {answers} --profile-name ci")

        assert rc == 1
        out = capsys.readouterr().out
        assert "monitring" in out
        assert "quota.monthly_limt" in out
        assert not (config_paths / "ci.json").exists()

    def test_azure_client_secret_in_file_rejected(self, config_paths, tester, tmp_path, capsys):
        """Confidential-client secrets are keyring-only — never accepted from the file."""
        answers = tmp_path / "answers.yaml"
        answers.write_text(
            """
okta:
  domain: login.microsoftonline.com/tenant-id/v2.0
  client_id: 12345678-1234-1234-1234-123456789012
azure_auth_mode: secret
client_secret: super-secret-value
"""
        )

        rc = tester.run(f"--from-file {answers} --profile-name ci")

        assert rc == 1
        out = capsys.readouterr().out
        assert "client_secret" in out
        assert "keyring" in out
        assert not (config_paths / "ci.json").exists()

    def test_missing_answers_file_fails(self, config_paths, tester, tmp_path, capsys):
        rc = tester.run(f"--from-file {tmp_path / 'nope.yaml'} --profile-name ci")

        assert rc == 1
        assert "not found" in capsys.readouterr().out

    def test_metering_requires_quota(self, config_paths, tester, tmp_path, capsys):
        answers = tmp_path / "answers.yaml"
        answers.write_text(MINIMAL_YAML + "quota:\n  enabled: false\nmetering:\n  enabled: true\n")

        rc = tester.run(f"--from-file {answers} --profile-name ci")

        assert rc == 1
        assert "metering.enabled" in capsys.readouterr().out
        assert not (config_paths / "ci.json").exists()


class TestFromFileWebSearchEntitlement:
    def test_entitled_groups_saved_to_profile(self, config_paths, tester, tmp_path):
        """web_search.entitled_groups + policy_mode flow into the profile fields."""
        answers = tmp_path / "answers.yaml"
        answers.write_text(
            MINIMAL_YAML
            + """
web_search:
  enabled: true
  entitled_groups:
    - bedrock-users
    - platform-eng
  policy_mode: ENFORCE
"""
        )

        rc = tester.run(f"--from-file {answers} --profile-name ci")

        assert rc == 0
        saved = _load_profile(config_paths, "ci")
        assert saved["web_search_enabled"] is True
        assert saved["websearch_entitled_groups"] == ["bedrock-users", "platform-eng"]
        assert saved["websearch_policy_mode"] == "ENFORCE"

    def test_entitlement_defaults_off_with_log_only(self, config_paths, tester, tmp_path):
        """Omitting the entitlement keys keeps the pre-entitlement behavior."""
        answers = tmp_path / "answers.yaml"
        answers.write_text(MINIMAL_YAML + "web_search:\n  enabled: true\n")

        rc = tester.run(f"--from-file {answers} --profile-name ci")

        assert rc == 0
        saved = _load_profile(config_paths, "ci")
        assert saved["websearch_entitled_groups"] == []
        assert saved["websearch_policy_mode"] == "LOG_ONLY"

    def test_invalid_policy_mode_rejected(self, config_paths, tester, tmp_path, capsys):
        answers = tmp_path / "answers.yaml"
        answers.write_text(MINIMAL_YAML + "web_search:\n  enabled: true\n  policy_mode: AUDIT\n")

        rc = tester.run(f"--from-file {answers} --profile-name ci")

        assert rc == 1
        assert "web_search.policy_mode" in capsys.readouterr().out
        assert not (config_paths / "ci.json").exists()

    def test_non_list_entitled_groups_rejected(self, config_paths, tester, tmp_path, capsys):
        answers = tmp_path / "answers.yaml"
        answers.write_text(MINIMAL_YAML + "web_search:\n  enabled: true\n  entitled_groups: bedrock-users\n")

        rc = tester.run(f"--from-file {answers} --profile-name ci")

        assert rc == 1
        assert "web_search.entitled_groups" in capsys.readouterr().out
        assert not (config_paths / "ci.json").exists()


class TestFromFileCollision:
    def test_existing_profile_requires_force(self, config_paths, tester, tmp_path, capsys):
        answers = tmp_path / "answers.yaml"
        answers.write_text(MINIMAL_YAML)

        assert tester.run(f"--from-file {answers} --profile-name ci") == 0
        rc = tester.run(f"--from-file {answers} --profile-name ci")

        assert rc == 1
        assert "--force" in capsys.readouterr().out

    def test_force_overwrites_existing_profile(self, config_paths, tester, tmp_path):
        answers = tmp_path / "answers.yaml"
        answers.write_text(MINIMAL_YAML)
        assert tester.run(f"--from-file {answers} --profile-name ci") == 0

        updated = tmp_path / "answers2.yaml"
        updated.write_text(MINIMAL_YAML + "aws:\n  region: us-west-2\n")
        rc = tester.run(f"--from-file {updated} --profile-name ci --force")

        assert rc == 0
        assert _load_profile(config_paths, "ci")["aws_region"] == "us-west-2"


class TestExportRoundTrip:
    def test_export_then_import_produces_equivalent_profile(self, config_paths, tester, tmp_path):
        """export-answers -> from-file must reproduce the profile exactly."""
        answers = tmp_path / "answers.yaml"
        answers.write_text(OKTA_COST_YAML)
        assert tester.run(f"--from-file {answers} --profile-name original") == 0

        exported = tmp_path / "exported.yaml"
        assert tester.run(f"--export-answers {exported} --profile-name original") == 0
        assert exported.exists()

        assert tester.run(f"--from-file {exported} --profile-name reimported") == 0

        volatile = {"name", "created_at", "updated_at"}
        original = {k: v for k, v in _load_profile(config_paths, "original").items() if k not in volatile}
        reimported = {k: v for k, v in _load_profile(config_paths, "reimported").items() if k not in volatile}
        assert original == reimported

    def test_export_unknown_profile_fails(self, config_paths, tester, tmp_path, capsys):
        rc = tester.run(f"--export-answers {tmp_path / 'out.yaml'} --profile-name ghost")

        assert rc == 1
        assert "not found" in capsys.readouterr().out.lower()

    def test_exported_file_is_valid_answers_schema(self, config_paths, tester, tmp_path):
        """The exported file must pass the same structural checks as a hand-written one."""
        from governed_inference_platform.cli.commands.init_answers import build_config_from_answers, load_answers_file

        answers = tmp_path / "answers.yaml"
        answers.write_text(MINIMAL_YAML)
        assert tester.run(f"--from-file {answers} --profile-name ci") == 0

        exported = tmp_path / "exported.yaml"
        assert tester.run(f"--export-answers {exported} --profile-name ci") == 0

        _, errors = build_config_from_answers(load_answers_file(exported))
        assert errors == []


class _ExplodingQuestionary:
    """Stand-in for the questionary module that fails on ANY attribute access."""

    def __getattr__(self, name):
        raise AssertionError(f"questionary.{name} was called during --from-file (non-interactive guarantee broken)")


class TestNonInteractiveGuarantee:
    def test_no_questionary_prompt_fires_with_from_file(self, config_paths, tester, tmp_path):
        """--from-file must never touch questionary — CI has no TTY."""
        answers = tmp_path / "answers.yaml"
        answers.write_text(MINIMAL_YAML)

        with patch("governed_inference_platform.cli.commands.init.questionary", new=_ExplodingQuestionary()):
            rc = tester.run(f"--from-file {answers} --profile-name ci")

        assert rc == 0
        assert (config_paths / "ci.json").exists()

    def test_no_questionary_prompt_fires_on_validation_failure(self, config_paths, tester, tmp_path):
        """Even the failure path must stay prompt-free (no fallback to the wizard)."""
        answers = tmp_path / "answers.yaml"
        answers.write_text("{}\n")

        with patch("governed_inference_platform.cli.commands.init.questionary", new=_ExplodingQuestionary()):
            rc = tester.run(f"--from-file {answers} --profile-name ci")

        assert rc == 1

    def test_no_questionary_prompt_fires_with_export_answers(self, config_paths, tester, tmp_path):
        answers = tmp_path / "answers.yaml"
        answers.write_text(MINIMAL_YAML)
        assert tester.run(f"--from-file {answers} --profile-name ci") == 0

        exported = tmp_path / "exported.yaml"
        with patch("governed_inference_platform.cli.commands.init.questionary", new=_ExplodingQuestionary()):
            rc = tester.run(f"--export-answers {exported} --profile-name ci")

        assert rc == 0


class TestAuthPathCoverage:
    """Answers files must work across all three auth modes (or fail clearly)."""

    def test_idc_answers_file(self, config_paths, tester, tmp_path):
        answers = tmp_path / "answers.yaml"
        answers.write_text(
            """
auth_type: idc
idc_start_url: https://company.awsapps.com/start
idc_account_id: "123456789012"
"""
        )

        rc = tester.run(f"--from-file {answers} --profile-name idc-ci")

        assert rc == 0
        saved = _load_profile(config_paths, "idc-ci")
        assert saved["auth_type"] == "idc"
        assert saved["sso_enabled"] is False
        assert saved["idc_start_url"] == "https://company.awsapps.com/start"
        assert saved["idc_permission_set_name"] == "BedrockDeveloperAccess"  # wizard default
        assert saved["quota_monitoring_enabled"] is False  # IDC wizard default
        assert saved["web_search_enabled"] is False  # forced off for IDC

    def test_idc_missing_fields_all_listed(self, config_paths, tester, tmp_path, capsys):
        answers = tmp_path / "answers.yaml"
        answers.write_text("auth_type: idc\n")

        rc = tester.run(f"--from-file {answers} --profile-name idc-ci")

        assert rc == 1
        out = capsys.readouterr().out
        assert "idc_start_url" in out
        assert "idc_account_id" in out

    def test_none_auth_answers_file(self, config_paths, tester, tmp_path):
        answers = tmp_path / "answers.yaml"
        answers.write_text("auth_type: none\n")

        rc = tester.run(f"--from-file {answers} --profile-name none-ci")

        assert rc == 0
        saved = _load_profile(config_paths, "none-ci")
        assert saved["auth_type"] == "none"
        assert saved["provider_domain"] == "none"
        assert saved["quota_monitoring_enabled"] is False  # no per-user identity

    def test_none_auth_with_quota_enabled_rejected(self, config_paths, tester, tmp_path, capsys):
        answers = tmp_path / "answers.yaml"
        answers.write_text("auth_type: none\nquota:\n  enabled: true\n")

        rc = tester.run(f"--from-file {answers} --profile-name none-ci")

        assert rc == 1
        assert "quota.enabled" in capsys.readouterr().out


GENERIC_OIDC_YAML = """
okta:
  domain: auth.example.com
  client_id: bedrock-cli-prod
provider_type: generic
oidc_issuer_url: https://auth.example.com
oidc_authorization_endpoint: https://auth.example.com/as/authorization.oauth2
oidc_token_endpoint: https://auth.example.com/as/token.oauth2
oidc_jwks_uri: https://auth.example.com/pf/JWKS
"""


class TestGenericOidcThumbprintOptional:
    """The IAM OIDC provider thumbprint is optional for generic OIDC answers files.

    IAM retrieves the CA thumbprint itself when the template omits ThumbprintList, so a
    GitOps answers file must not be forced to carry one. Files that do carry one (older
    pipelines, private-CA JWKS hosts) must keep working, normalized and validated.
    """

    def test_generic_oidc_without_thumbprint_creates_profile(self, config_paths, tester, tmp_path):
        """Regression: an answers file with the four endpoints but no oidc_thumbprint must succeed."""
        answers = tmp_path / "answers.yaml"
        answers.write_text(GENERIC_OIDC_YAML)

        rc = tester.run(f"--from-file {answers} --profile-name generic-ci")

        assert rc == 0
        saved = _load_profile(config_paths, "generic-ci")
        assert saved["provider_type"] == "generic"
        assert saved["oidc_issuer_url"] == "https://auth.example.com"
        assert saved["oidc_jwks_uri"] == "https://auth.example.com/pf/JWKS"
        assert saved["oidc_thumbprint"] is None

    def test_generic_oidc_with_thumbprint_is_normalized_and_kept(self, config_paths, tester, tmp_path):
        """Backward compat: a supplied thumbprint (colons, upper-case) is normalized and saved."""
        answers = tmp_path / "answers.yaml"
        answers.write_text(
            GENERIC_OIDC_YAML + 'oidc_thumbprint: "9E:99:A4:8A:99:60:B1:49:26:BB:7F:3B:02:E2:2D:A2:B0:AB:72:80"\n'
        )

        rc = tester.run(f"--from-file {answers} --profile-name generic-ci")

        assert rc == 0
        saved = _load_profile(config_paths, "generic-ci")
        assert saved["oidc_thumbprint"] == "9e99a48a9960b14926bb7f3b02e22da2b0ab7280"

    def test_generic_oidc_malformed_thumbprint_rejected(self, config_paths, tester, tmp_path, capsys):
        """A present-but-malformed thumbprint is still a hard error pointing at the IAM guide."""
        answers = tmp_path / "answers.yaml"
        answers.write_text(GENERIC_OIDC_YAML + "oidc_thumbprint: not-a-thumbprint\n")

        rc = tester.run(f"--from-file {answers} --profile-name generic-ci")

        assert rc == 1
        out = capsys.readouterr().out
        assert "oidc_thumbprint" in out
        assert "id_roles_providers_create_oidc_verify-thumbprint" in out
        assert not (config_paths / "generic-ci.json").exists()

    def test_generic_oidc_endpoints_still_required(self, config_paths, tester, tmp_path, capsys):
        """Dropping the thumbprint requirement must not loosen the endpoint requirements."""
        answers = tmp_path / "answers.yaml"
        answers.write_text(
            """
okta:
  domain: auth.example.com
  client_id: bedrock-cli-prod
provider_type: generic
"""
        )

        rc = tester.run(f"--from-file {answers} --profile-name generic-ci")

        assert rc == 1
        out = capsys.readouterr().out
        for field in ("oidc_issuer_url", "oidc_authorization_endpoint", "oidc_token_endpoint", "oidc_jwks_uri"):
            assert field in out
        assert "oidc_thumbprint" not in out
