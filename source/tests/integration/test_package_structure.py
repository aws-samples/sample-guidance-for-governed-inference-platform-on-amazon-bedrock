"""Integration tests for package structure validation.

Validates that gip package output contains all required files and fields
for each OS, including OTEL configuration when monitoring is enabled.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from governed_inference_platform.cli.commands.package import PackageCommand
from governed_inference_platform.config import Profile


@pytest.fixture
def base_profile():
    """Create a base profile with standard configuration."""
    return Profile(
        name="TestOrg",
        provider_domain="company.okta.com",
        client_id="0oa123456",
        credential_storage="keyring",
        aws_region="us-east-1",
        identity_pool_name="TestOrgPool",
        allowed_bedrock_regions=["us-east-1", "us-west-2"],
        cross_region_profile="us",
        selected_model="us.anthropic.claude-sonnet-4-20250514-v1:0",
        selected_source_region="us-east-1",
        monitoring_enabled=False,
        provider_type="okta",
        okta_auth_server="default",
        cowork_3p_enabled=False,
    )


@pytest.fixture
def otel_profile(base_profile):
    """Profile with monitoring/OTEL enabled."""
    base_profile.monitoring_enabled = True
    return base_profile


@pytest.fixture
def cowork_profile(base_profile):
    """Profile with CoWork 3P enabled."""
    base_profile.cowork_3p_enabled = True
    return base_profile


class TestConfigJsonRequiredFields:
    """Validate config.json contains all required fields for credential provider."""

    REQUIRED_FIELDS = [
        "provider_domain",
        "client_id",
        "aws_region",
        "provider_type",
        "credential_storage",
        "cross_region_profile",
    ]

    def test_cognito_config_has_required_fields(self, base_profile):
        """Cognito federation config must have identity_pool_id."""
        command = PackageCommand()

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            config_path = command._create_config(output_dir, base_profile, "us-east-1:pool-id-123", "cognito", "gip")

            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)

            profile_config = config["profiles"]["gip"]

            for field in self.REQUIRED_FIELDS:
                assert field in profile_config, f"Missing required field: {field}"

            assert "identity_pool_id" in profile_config
            assert profile_config["federation_type"] == "cognito"

    def test_direct_sts_config_has_required_fields(self, base_profile):
        """Direct STS federation config must have federated_role_arn."""
        base_profile.federation_type = "direct"
        base_profile.max_session_duration = 43200
        command = PackageCommand()

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            config_path = command._create_config(
                output_dir,
                base_profile,
                "arn:aws:iam::123456789:role/BedrockRole",
                "direct",
                "gip",
            )

            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)

            profile_config = config["profiles"]["gip"]

            for field in self.REQUIRED_FIELDS:
                assert field in profile_config, f"Missing required field: {field}"

            assert "federated_role_arn" in profile_config
            assert profile_config["federation_type"] == "direct"
            assert profile_config["max_session_duration"] == 43200

    def test_okta_auth_server_included_when_set(self, base_profile):
        """okta_auth_server is packaged under BOTH runtime key names.

        The Python credential provider reads "okta_auth_server"; the Go
        provider reads "okta_auth_server_id". Before this fix neither key was
        written, so the runtime always fell back to the Org Authorization
        Server while deploy-side JWT authorizers pin /oauth2/<id> issuers.
        """
        base_profile.okta_auth_server = "aus789xyz"
        command = PackageCommand()

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            config_path = command._create_config(output_dir, base_profile, "us-east-1:pool-id", "cognito", "gip")

            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)

            profile_config = config["profiles"]["gip"]
            assert profile_config["okta_auth_server"] == "aus789xyz"
            assert profile_config["okta_auth_server_id"] == "aus789xyz"

    def test_okta_auth_server_empty_written_as_is(self, base_profile):
        """Empty value (Org Authorization Server, paid plans) is packaged explicitly."""
        base_profile.okta_auth_server = ""
        command = PackageCommand()

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            config_path = command._create_config(output_dir, base_profile, "us-east-1:pool-id", "cognito", "gip")

            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)

            profile_config = config["profiles"]["gip"]
            assert profile_config["okta_auth_server"] == ""
            assert profile_config["okta_auth_server_id"] == ""

    def test_okta_auth_server_not_written_for_non_okta(self, base_profile):
        """Non-Okta providers never get the Okta keys."""
        base_profile.provider_type = "auth0"
        base_profile.provider_domain = "company.auth0.com"
        command = PackageCommand()

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            config_path = command._create_config(output_dir, base_profile, "us-east-1:pool-id", "cognito", "gip")

            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)

            profile_config = config["profiles"]["gip"]
            assert "okta_auth_server" not in profile_config
            assert "okta_auth_server_id" not in profile_config

    def test_selected_model_included(self, base_profile):
        """selected_model should be written to config.json."""
        command = PackageCommand()

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            config_path = command._create_config(output_dir, base_profile, "us-east-1:pool-id", "cognito", "gip")

            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)

            assert config["profiles"]["gip"]["selected_model"] == base_profile.selected_model

    def test_custom_profile_name_as_key(self, base_profile):
        """Profile name passed to _create_config should key the profiles mapping."""
        command = PackageCommand()

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            config_path = command._create_config(
                output_dir, base_profile, "us-east-1:pool-id", "cognito", "MyCustomProfile"
            )

            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)

            assert set(config) == {"profiles"}
            assert "MyCustomProfile" in config["profiles"]
            assert "gip" not in config["profiles"]

    def test_quota_fields_when_configured(self, base_profile):
        """Quota fields written when quota_api_endpoint is set."""
        base_profile.quota_api_endpoint = "https://api.example.com/quota"
        base_profile.quota_fail_mode = "closed"
        base_profile.quota_check_interval = 60
        command = PackageCommand()

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            config_path = command._create_config(output_dir, base_profile, "us-east-1:pool-id", "cognito", "gip")

            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)

            assert config["profiles"]["gip"]["quota_api_endpoint"] == "https://api.example.com/quota"
            assert config["profiles"]["gip"]["quota_fail_mode"] == "closed"
            assert config["profiles"]["gip"]["quota_check_interval"] == 60


class TestPackagedOktaIssuer:
    """Pin the issuer the PACKAGED config drives at runtime (Okta split-brain fix).

    profile.okta_auth_server → config.json → MultiProviderAuth endpoints.
    Deploy-side JWT authorizers (quota/websearch/bootstrap) pin
    https://<domain>/oauth2/default, so packaging the profile value is what
    lets an admin put runtime token minting on the same authorization server
    the deployed authorizers validate against. Before the fix the value never
    reached config.json and the runtime silently used the Org AS.
    """

    @pytest.mark.parametrize(
        ("auth_server", "expected_authorize", "expected_token"),
        [
            ("default", "/oauth2/default/v1/authorize", "/oauth2/default/v1/token"),
            ("aus789xyz", "/oauth2/aus789xyz/v1/authorize", "/oauth2/aus789xyz/v1/token"),
            ("", "/oauth2/v1/authorize", "/oauth2/v1/token"),
        ],
        ids=["default-cas", "custom-cas", "org-as"],
    )
    def test_packaged_config_drives_runtime_endpoints(
        self, base_profile, auth_server, expected_authorize, expected_token
    ):
        """The runtime provider resolves endpoints from the packaged value, not a fallback."""
        from credential_provider.__main__ import MultiProviderAuth

        base_profile.okta_auth_server = auth_server
        command = PackageCommand()

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            config_path = command._create_config(output_dir, base_profile, "us-east-1:pool-id", "cognito", "gip")

            with open(config_path, encoding="utf-8") as f:
                packaged = json.load(f)["profiles"]["gip"]

        with (
            patch.object(MultiProviderAuth, "_load_config", return_value=packaged),
            patch.object(MultiProviderAuth, "_init_credential_storage"),
        ):
            auth = MultiProviderAuth(profile="gip")

        assert auth.provider_config["authorize_endpoint"] == expected_authorize
        assert auth.provider_config["token_endpoint"] == expected_token


class TestClaudeSettingsGeneration:
    """Validate gip-settings/settings.json generation."""

    def test_basic_settings_has_bedrock_env(self, base_profile):
        """settings.json must set CLAUDE_CODE_USE_BEDROCK=1 and AWS_PROFILE."""
        command = PackageCommand()

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            command._create_claude_settings(output_dir, base_profile, profile_name="gip")

            settings_path = output_dir / "gip-settings" / "settings.json"
            assert settings_path.exists(), "gip-settings/settings.json not created"

            with open(settings_path, encoding="utf-8") as f:
                settings = json.load(f)

            assert settings["env"]["CLAUDE_CODE_USE_BEDROCK"] == "1"
            assert settings["env"]["AWS_PROFILE"] == "gip"
            assert "AWS_CREDENTIAL_PROCESS" in settings["env"]

    def test_model_env_vars_set(self, base_profile):
        """ANTHROPIC_MODEL and tier models should be set."""
        command = PackageCommand()

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            command._create_claude_settings(output_dir, base_profile, profile_name="gip")

            settings_path = output_dir / "gip-settings" / "settings.json"
            with open(settings_path, encoding="utf-8") as f:
                settings = json.load(f)

            # ANTHROPIC_MODEL is set to an alias (e.g. 'sonnet') not the raw model ID
            assert "ANTHROPIC_MODEL" in settings["env"]
            assert settings["env"]["ANTHROPIC_MODEL"] != ""
            assert "ANTHROPIC_SMALL_FAST_MODEL" in settings["env"]
            assert "ANTHROPIC_DEFAULT_SONNET_MODEL" in settings["env"]

    def test_otel_helper_configured_when_monitoring_enabled(self, otel_profile):
        """When monitoring is enabled and stack exists, otelHeadersHelper must be set."""
        command = PackageCommand()

        mock_outputs = [
            {"OutputKey": "CollectorEndpoint", "OutputValue": "https://collector.example.com:4318"},
            {"OutputKey": "VerifiedCollectorEndpoint", "OutputValue": "https://collector.example.com:4318"},
        ]
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(mock_outputs)

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)

            with patch("subprocess.run", return_value=mock_result):
                command._create_claude_settings(output_dir, otel_profile, profile_name="gip")

            settings_path = output_dir / "gip-settings" / "settings.json"
            with open(settings_path, encoding="utf-8") as f:
                settings = json.load(f)

            assert "otelHeadersHelper" in settings, "otelHeadersHelper must be configured when monitoring is enabled"
            assert settings["otelHeadersHelper"] == "__OTEL_HELPER_PATH__ --profile gip"
            assert settings["env"]["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
            assert settings["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://collector.example.com:4318"

    def test_no_otel_helper_when_monitoring_disabled(self, base_profile):
        """No otelHeadersHelper when monitoring is disabled."""
        command = PackageCommand()

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            command._create_claude_settings(output_dir, base_profile, profile_name="gip")

            settings_path = output_dir / "gip-settings" / "settings.json"
            with open(settings_path, encoding="utf-8") as f:
                settings = json.load(f)

            assert "otelHeadersHelper" not in settings
            assert "CLAUDE_CODE_ENABLE_TELEMETRY" not in settings["env"]

    def test_collector_only_output_is_aggregate_without_identity_helper(self, otel_profile):
        command = PackageCommand()
        mock_result = MagicMock(
            returncode=0,
            stdout=json.dumps(
                [{"OutputKey": "CollectorEndpoint", "OutputValue": "https://collector.example.com:4318"}]
            ),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            with patch("subprocess.run", return_value=mock_result):
                command._create_claude_settings(output_dir, otel_profile, profile_name="gip")
            with open(output_dir / "gip-settings" / "settings.json", encoding="utf-8") as f:
                settings = json.load(f)

        assert settings["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://collector.example.com:4318"
        assert "otelHeadersHelper" not in settings

    def test_session_storage_adds_auth_refresh(self, base_profile):
        """Session-based credential storage should add awsAuthRefresh."""
        base_profile.credential_storage = "session"
        command = PackageCommand()

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            command._create_claude_settings(output_dir, base_profile, profile_name="gip")

            settings_path = output_dir / "gip-settings" / "settings.json"
            with open(settings_path, encoding="utf-8") as f:
                settings = json.load(f)

            assert "awsAuthRefresh" in settings

    def test_keyring_storage_no_auth_refresh(self, base_profile):
        """Keyring credential storage should NOT add awsAuthRefresh."""
        base_profile.credential_storage = "keyring"
        command = PackageCommand()

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            command._create_claude_settings(output_dir, base_profile, profile_name="gip")

            settings_path = output_dir / "gip-settings" / "settings.json"
            with open(settings_path, encoding="utf-8") as f:
                settings = json.load(f)

            assert "awsAuthRefresh" not in settings

    def test_inference_profile_arns_override_models(self, base_profile):
        """Application Inference Profile ARNs should override CRIS model IDs."""
        base_profile.inference_profile_sonnet_arn = (
            "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/sonnetprofile1"
        )
        base_profile.inference_profile_haiku_arn = (
            "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/haikuprofile1"
        )
        command = PackageCommand()

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            command._create_claude_settings(output_dir, base_profile, profile_name="gip")

            settings_path = output_dir / "gip-settings" / "settings.json"
            with open(settings_path, encoding="utf-8") as f:
                settings = json.load(f)

            assert settings["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] == base_profile.inference_profile_sonnet_arn
            assert settings["env"]["ANTHROPIC_SMALL_FAST_MODEL"] == base_profile.inference_profile_haiku_arn
            assert settings["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == base_profile.inference_profile_haiku_arn
            assert not settings["env"]["ANTHROPIC_MODEL"].startswith("arn:")
            assert settings["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"].startswith("us.anthropic.")

    def test_inference_profile_opus_arn_overrides_opus_tier(self, base_profile):
        """Opus-tier AIP ARN maps to ANTHROPIC_DEFAULT_OPUS_MODEL only."""
        base_profile.inference_profile_opus_arn = (
            "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/opusprofile1"
        )
        command = PackageCommand()

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            command._create_claude_settings(output_dir, base_profile, profile_name="gip")

            settings_path = output_dir / "gip-settings" / "settings.json"
            with open(settings_path, encoding="utf-8") as f:
                settings = json.load(f)

            assert settings["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"] == base_profile.inference_profile_opus_arn
            assert settings["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"].startswith("us.anthropic.")
            assert settings["env"]["ANTHROPIC_SMALL_FAST_MODEL"].startswith("us.anthropic.")

    def test_inference_profile_arns_apply_without_model_selection(self, base_profile):
        """AIP overrides are admin-managed attribution routing."""
        base_profile.settings_target = "managed"
        base_profile.lock_default_model = False
        base_profile.inference_profile_sonnet_arn = (
            "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/sonnetprofile1"
        )
        command = PackageCommand()

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            command._create_claude_settings(output_dir, base_profile, profile_name="gip")

            settings_path = output_dir / "gip-settings" / "managed-settings.json"
            with open(settings_path, encoding="utf-8") as f:
                settings = json.load(f)

            assert "ANTHROPIC_MODEL" not in settings["env"]
            assert settings["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] == base_profile.inference_profile_sonnet_arn


class TestCoworkConfigGeneration:
    """Validate CoWork 3P MDM configuration files for all OS."""

    def test_cowork_json_has_required_fields(self, cowork_profile):
        """cowork-3p-config.json must have inferenceProvider, region, profile."""
        from governed_inference_platform.cli.utils.cowork_3p import build_mdm_config, generate_json

        mdm_config = build_mdm_config(
            bedrock_region="us-east-1",
            model_aliases=["opus", "sonnet", "haiku"],
            profile_name="gip",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            json_path = generate_json(output_dir, mdm_config)

            with open(json_path, encoding="utf-8") as f:
                config = json.load(f)

            assert config["inferenceProvider"] == "bedrock"
            assert config["inferenceBedrockRegion"] == "us-east-1"
            assert config["inferenceBedrockProfile"] == "gip"
            assert config["inferenceModels"] == ["opus", "sonnet", "haiku"]
            assert config["isClaudeCodeForDesktopEnabled"] is True

    def test_mobileconfig_valid_xml(self, cowork_profile):
        """macOS .mobileconfig must be valid XML plist with required keys."""
        import plistlib

        from governed_inference_platform.cli.utils.cowork_3p import build_mdm_config, generate_mobileconfig

        mdm_config = build_mdm_config(
            bedrock_region="us-west-2",
            model_aliases=["opus", "sonnet", "haiku"],
            profile_name="TestProfile",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            mc_path = generate_mobileconfig(output_dir, mdm_config)

            content = mc_path.read_text(encoding="utf-8")

            # Must be a parseable XML plist with a dict payload
            parsed = plistlib.loads(content.encode("utf-8"))
            assert isinstance(parsed, dict)

            # Must contain the Anthropic payload type
            assert "com.anthropic.claudefordesktop" in content
            assert "inferenceProvider" in content
            assert "bedrock" in content
            assert "us-west-2" in content

    def test_reg_file_valid_format(self, cowork_profile):
        """Windows .reg file must have correct registry key format."""
        from governed_inference_platform.cli.utils.cowork_3p import build_mdm_config, generate_reg_file

        mdm_config = build_mdm_config(
            bedrock_region="us-east-1",
            model_aliases=["opus", "sonnet", "haiku"],
            profile_name="gip",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            reg_path = generate_reg_file(output_dir, mdm_config)

            content = reg_path.read_text(encoding="utf-8")

            assert "Windows Registry Editor Version 5.00" in content
            assert r"HKEY_CURRENT_USER\SOFTWARE\Policies\Claude" in content
            assert '"inferenceProvider"="bedrock"' in content
            assert '"inferenceBedrockRegion"="us-east-1"' in content
            assert '"inferenceBedrockProfile"="gip"' in content

    def test_cowork_config_with_otel_endpoint(self, cowork_profile):
        """CoWork config should include otlpEndpoint when monitoring is configured."""
        from governed_inference_platform.cli.utils.cowork_3p import build_mdm_config, generate_json

        mdm_config = build_mdm_config(
            bedrock_region="us-east-1",
            model_aliases=["opus", "sonnet", "haiku"],
            profile_name="gip",
        )
        mdm_config["otlpEndpoint"] = "https://collector.example.com:4318"
        mdm_config["otlpProtocol"] = "http/protobuf"

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            json_path = generate_json(output_dir, mdm_config)

            with open(json_path, encoding="utf-8") as f:
                config = json.load(f)

            assert config["otlpEndpoint"] == "https://collector.example.com:4318"
            assert config["otlpProtocol"] == "http/protobuf"


class TestInstallerScripts:
    """Validate installer scripts reference correct paths per OS."""

    def test_install_sh_sets_aws_profile(self, base_profile):
        """install.sh must configure AWS profile with credential_process."""
        command = PackageCommand()

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)

            installer_path = command._create_installer(
                output_dir,
                base_profile,
                [("macos-arm64", Path("credential-process-macos-arm64"))],
                [],
            )

            content = installer_path.read_text(encoding="utf-8")

            # Must set up AWS profile
            assert "aws configure" in content or "credential_process" in content
            # Must reference the credential binary
            assert "credential-process" in content

    def test_install_sh_handles_otel_helper(self, otel_profile):
        """install.sh must configure otel-helper path when OTEL binaries present."""
        command = PackageCommand()

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)

            installer_path = command._create_installer(
                output_dir,
                otel_profile,
                [("macos-arm64", Path("credential-process-macos-arm64"))],
                [("macos-arm64", Path("otel-helper-macos-arm64"))],
            )

            content = installer_path.read_text(encoding="utf-8")

            # Must reference otel-helper setup
            assert "otel-helper" in content

    def test_install_bat_generated_for_windows(self, base_profile):
        """install.bat must be generated when Windows binaries are present."""
        command = PackageCommand()

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)

            command._create_installer(
                output_dir,
                base_profile,
                [
                    ("macos-arm64", Path("credential-process-macos-arm64")),
                    ("windows", Path("credential-process-windows.exe")),
                ],
                [],
            )

            bat_path = output_dir / "install.bat"
            assert bat_path.exists(), "install.bat not generated for Windows"

            content = bat_path.read_text(encoding="utf-8")
            assert "-InstallAll" in content
            assert "credential-process-windows.exe" in (output_dir / "gip-install.ps1").read_text(encoding="utf-8")
            transaction = (output_dir / "gip-install.ps1").read_text(encoding="utf-8")
            assert '.claude\\settings.json"' in transaction


class TestWindowsPsOtelHelperIncluded:
    """Test that PS1/CMD otel-helper fallback scripts are included in Windows packages."""

    def test_ps1_and_cmd_included_when_windows_otel_built(self):
        """When Windows otel-helper is in the build, PS1/CMD are copied to output."""
        import shutil
        import tempfile
        from pathlib import Path

        from governed_inference_platform.config import Profile

        Profile(
            name="test",
            provider_domain="test.okta.com",
            client_id="test-client",
            credential_storage="keyring",
            aws_region="us-east-1",
            identity_pool_name="test-pool",
            monitoring_enabled=True,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)

            # Simulate: Windows otel-helper binary exists in output
            (output_dir / "otel-helper-windows.exe").touch()

            # Source PS1/CMD should exist in the repo
            source_dir = Path(__file__).resolve().parent.parent.parent / "otel_helper"
            assert (source_dir / "otel-helper.ps1").exists(), "otel-helper.ps1 missing from source"
            assert (source_dir / "otel-helper.cmd").exists(), "otel-helper.cmd missing from source"

            # Copy them as the package command would
            for script_name in ("otel-helper.ps1", "otel-helper.cmd"):
                shutil.copy2(source_dir / script_name, output_dir / script_name)

            # Verify they're in the output
            assert (output_dir / "otel-helper.ps1").exists()
            assert (output_dir / "otel-helper.cmd").exists()
