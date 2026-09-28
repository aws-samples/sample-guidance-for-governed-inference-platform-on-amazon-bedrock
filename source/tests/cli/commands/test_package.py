# ABOUTME: Unit tests for package command with cross-region support
# ABOUTME: Tests that package command properly includes cross-region configuration

"""Tests for the package command."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from cleo.testers.command_tester import CommandTester

from governed_inference_platform.cli.commands.package import (
    PackageCommand,
    _copy_windows_otel_fallbacks,
    _default_go_platforms,
)
from governed_inference_platform.config import Profile


def test_deprecated_prebuilt_options_remain_accepted_as_no_ops(capsys):
    command = PackageCommand()
    command._check_build_status = MagicMock(return_value=0)
    tester = CommandTester(command)

    assert tester.execute("--status latest --build-local --no-cache") == 0
    output = capsys.readouterr().out
    assert "--build-local is deprecated and has no effect" in output
    assert "--no-cache is deprecated and has no effect" in output


def test_default_go_platforms_are_host_aware():
    assert _default_go_platforms("Linux") == ["linux-x64", "linux-arm64", "windows"]
    assert _default_go_platforms("Windows") == ["linux-x64", "linux-arm64", "windows"]
    assert _default_go_platforms("Darwin") == [
        "macos-arm64",
        "macos-intel",
        "linux-x64",
        "linux-arm64",
        "windows",
    ]


def test_windows_monitoring_fallbacks_are_required(tmp_path):
    source_dir = tmp_path / "source"
    output_dir = tmp_path / "output"
    source_dir.mkdir()
    output_dir.mkdir()
    (source_dir / "otel-helper.ps1").write_text("powershell")

    with pytest.raises(FileNotFoundError, match="otel-helper.cmd"):
        _copy_windows_otel_fallbacks(output_dir, source_dir)

    assert not (output_dir / "otel-helper.ps1").exists()


def test_go_build_rejects_macos_target_from_non_macos_host(tmp_path):
    command = PackageCommand()
    command.line = MagicMock()

    with (
        patch("governed_inference_platform.cli.commands.package.platform.system", return_value="Linux"),
        patch(
            "governed_inference_platform.cli.commands.package.subprocess.run",
            return_value=MagicMock(stdout="go version go1.24.0 linux/amd64"),
        ),
        pytest.raises(RuntimeError, match="macOS credential binaries require CGO"),
    ):
        command._build_go_binaries(tmp_path, ["macos-arm64"], monitoring_enabled=False)


def test_go_build_fails_when_any_requested_binary_fails(tmp_path):
    command = PackageCommand()
    command.line = MagicMock()
    go_version = MagicMock(stdout="go version go1.24.0 linux/amd64")
    failed_build = MagicMock(returncode=1, stderr="compile failed")

    with (
        patch("governed_inference_platform.cli.commands.package.platform.system", return_value="Linux"),
        patch(
            "governed_inference_platform.cli.commands.package.subprocess.run",
            side_effect=[go_version, failed_build],
        ),
        patch("governed_inference_platform.cli.commands.package._go_ldflags", return_value=""),
        pytest.raises(RuntimeError, match="credential-process-linux-x64: compile failed"),
    ):
        command._build_go_binaries(tmp_path, ["linux-x64"], monitoring_enabled=False)


def test_go_helper_failure_removes_staged_and_existing_outputs(tmp_path):
    command = PackageCommand()
    command.line = MagicMock()
    final_credential = tmp_path / "credential-process-linux-x64"
    final_credential.write_text("stale")

    def fake_run(cmd, **kwargs):
        if cmd == ["go", "version"]:
            return MagicMock(stdout="go version go1.24.0 linux/amd64")
        staged_path = Path(cmd[cmd.index("-o") + 1])
        if "credential-process" in cmd[-1]:
            staged_path.write_text("new")
            return MagicMock(returncode=0, stderr="")
        return MagicMock(returncode=1, stderr="helper compile failed")

    with (
        patch("governed_inference_platform.cli.commands.package.platform.system", return_value="Linux"),
        patch("governed_inference_platform.cli.commands.package.subprocess.run", side_effect=fake_run),
        patch("governed_inference_platform.cli.commands.package._go_ldflags", return_value=""),
        pytest.raises(RuntimeError, match="otel-helper-linux-x64: helper compile failed"),
    ):
        command._build_go_binaries(tmp_path, ["linux-x64"], monitoring_enabled=True)

    assert not final_credential.exists()
    assert not (tmp_path / ".credential-process-linux-x64.building").exists()
    assert not (tmp_path / ".otel-helper-linux-x64.building").exists()


def test_go_commit_failure_removes_staged_outputs(tmp_path):
    command = PackageCommand()
    command.line = MagicMock()

    def fake_run(cmd, **kwargs):
        if cmd == ["go", "version"]:
            return MagicMock(stdout="go version go1.24.0 linux/amd64")
        Path(cmd[cmd.index("-o") + 1]).write_text("binary")
        return MagicMock(returncode=0, stderr="")

    with (
        patch("governed_inference_platform.cli.commands.package.platform.system", return_value="Linux"),
        patch("governed_inference_platform.cli.commands.package.subprocess.run", side_effect=fake_run),
        patch("governed_inference_platform.cli.commands.package._go_ldflags", return_value=""),
        patch.object(Path, "replace", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        command._build_go_binaries(tmp_path, ["linux-x64"], monitoring_enabled=False)

    assert not (tmp_path / "credential-process-linux-x64").exists()
    assert not (tmp_path / ".credential-process-linux-x64.building").exists()


class TestPackageCommandCrossRegion:
    """Tests for package command cross-region functionality."""

    def test_config_includes_cross_region_profile(self):
        """Test that generated config.json includes cross_region_profile."""
        command = PackageCommand()

        # Create a test profile with cross-region settings
        profile = Profile(
            name="test",
            provider_domain="test.okta.com",
            client_id="test-client-id",
            credential_storage="keyring",
            aws_region="us-east-1",
            identity_pool_name="test-pool",
            allowed_bedrock_regions=["us-east-1", "us-east-2", "us-west-2"],
            cross_region_profile="us",
            monitoring_enabled=False,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)

            # Call _create_config
            config_path = command._create_config(output_dir, profile, "test-identity-pool-id")

            # Read and verify the config
            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)

            assert set(config) == {"profiles"}
            assert "gip" in config["profiles"]
            claude_config = config["profiles"]["gip"]

            # Check all expected fields
            assert claude_config["provider_domain"] == "test.okta.com"
            assert claude_config["client_id"] == "test-client-id"
            assert claude_config["identity_pool_id"] == "test-identity-pool-id"
            assert claude_config["aws_region"] == "us-east-1"
            assert claude_config["cross_region_profile"] == "us"
            assert claude_config["credential_storage"] == "keyring"

    def test_config_defaults_cross_region_to_us(self):
        """Test that config defaults cross_region_profile to 'us' if not set."""
        command = PackageCommand()

        # Create profile without cross_region_profile
        profile = Profile(
            name="test",
            provider_domain="test.okta.com",
            client_id="test-client",
            credential_storage="session",
            aws_region="us-west-2",
            identity_pool_name="test-pool",
            allowed_bedrock_regions=["us-east-1", "us-west-2"],
            monitoring_enabled=False,
        )
        # Explicitly set to None to test default
        profile.cross_region_profile = None

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)

            # Call _create_config
            config_path = command._create_config(output_dir, profile, "test-pool-id")

            # Read and verify
            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)

            # Should default to 'us'
            assert config["profiles"]["gip"]["cross_region_profile"] == "us"

    def test_installer_script_preserves_region(self):
        """Test that installer script correctly extracts region from config."""
        command = PackageCommand()

        profile = Profile(
            name="test",
            provider_domain="test.okta.com",
            client_id="test-client",
            credential_storage="session",
            aws_region="us-west-2",  # Note: different from cross-region
            identity_pool_name="test-pool",
            allowed_bedrock_regions=["us-east-1", "us-east-2", "us-west-2"],
            cross_region_profile="us",
            monitoring_enabled=False,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)

            # Create installer
            installer_path = command._create_installer(
                output_dir, profile, [("macos", Path("credential-process-macos"))], []
            )

            # Read installer and check region extraction
            with open(installer_path, encoding="utf-8") as f:
                installer_content = f.read()

            # Should extract region from Claude settings first, then fallback to profile region
            assert "AWS_REGION" in installer_content or "aws_region" in installer_content
            # The fallback should now have the interpolated region value
            assert "us-west-2" in installer_content or "config.json" in installer_content

    def test_macos_keychain_notice_is_gated_by_packaged_storage(self, tmp_path):
        profile = Profile(
            name="gip",
            provider_domain="test.okta.com",
            client_id="test-client",
            credential_storage="session",
            aws_region="us-east-1",
            identity_pool_name="test-pool",
            monitoring_enabled=False,
        )
        content = PackageCommand()._create_installer(tmp_path, profile, [], []).read_text(encoding="utf-8")
        assert "credential_storage') == 'keyring'" in content
        assert "macOS Keychain Access:" in content


class TestResolveFederation:
    """Regression tests for federation identifier resolution.

    Guards against the bug where `gip package` used `identity_pool_name` (a
    human-readable name) as the Cognito identity pool ID, producing a config.json
    that failed Cognito GetId validation ([\\w-]+:[0-9a-f-]+).
    """

    def _profile(self, **overrides):
        defaults = {
            "name": "test",
            "provider_domain": "example.auth.us-east-1.amazoncognito.com",
            "client_id": "test-client-id",
            "credential_storage": "keyring",
            "aws_region": "us-east-1",
            "identity_pool_name": "gip-auth",  # a NAME, not the pool ID
            "federation_type": "cognito",
            "monitoring_enabled": False,
        }
        defaults.update(overrides)
        return Profile(**defaults)

    def test_cognito_resolves_pool_id_from_stack_not_name(self):
        """Cognito must use the stack's IdentityPoolId (region:uuid), never the
        identity_pool_name. Core regression guard for the GetId ValidationException."""
        command = PackageCommand()
        profile = self._profile()
        console = MagicMock()
        fake_outputs = {
            "FederationType": "cognito",
            "IdentityPoolId": "us-east-1:00000000-0000-0000-0000-000000000000",
        }
        with patch(
            "governed_inference_platform.cli.commands.package.get_stack_outputs",
            return_value=fake_outputs,
        ):
            federation_type, identity_pool_id, federated_role_arn = command._resolve_federation(profile, console)

        assert federation_type == "cognito"
        assert identity_pool_id == "us-east-1:00000000-0000-0000-0000-000000000000"
        # Must NOT fall back to the human-readable name (the original bug)
        assert identity_pool_id != "gip-auth"
        assert federated_role_arn is None

    def test_direct_uses_profile_role_arn_without_stack(self):
        """Direct STS keeps the profile shortcut — the profile stores the real ARN."""
        command = PackageCommand()
        arn = "arn:aws:iam::123456789012:role/BedrockRole"
        profile = self._profile(federation_type="direct", federated_role_arn=arn)
        console = MagicMock()
        with patch("governed_inference_platform.cli.commands.package.get_stack_outputs") as mock_stack:
            federation_type, identity_pool_id, federated_role_arn = command._resolve_federation(profile, console)

        assert federation_type == "direct"
        assert federated_role_arn == arn
        assert identity_pool_id is None
        mock_stack.assert_not_called()  # no stack lookup needed for direct shortcut

    def test_cognito_missing_pool_id_returns_none(self):
        """If the stack has no IdentityPoolId, resolution fails gracefully (no name fallback)."""
        command = PackageCommand()
        profile = self._profile()
        console = MagicMock()
        with patch(
            "governed_inference_platform.cli.commands.package.get_stack_outputs",
            return_value={"FederationType": "cognito"},
        ):
            federation_type, identity_pool_id, federated_role_arn = command._resolve_federation(profile, console)

        assert identity_pool_id is None
        assert federated_role_arn is None

    def test_sso_disabled_returns_no_identifier(self):
        """SSO disabled needs no federation identifier and performs no stack lookup."""
        command = PackageCommand()
        profile = self._profile(sso_enabled=False)
        console = MagicMock()
        with patch("governed_inference_platform.cli.commands.package.get_stack_outputs") as mock_stack:
            federation_type, identity_pool_id, federated_role_arn = command._resolve_federation(profile, console)

        assert identity_pool_id is None
        assert federated_role_arn is None
        mock_stack.assert_not_called()


class TestPackageCommandOtelDefaults:
    """Tests for the default OTEL_RESOURCE_ATTRIBUTES written into settings.json."""

    def _make_monitoring_profile(self):
        return Profile(
            name="test",
            provider_domain="test.okta.com",
            client_id="test-client",
            credential_storage="session",
            aws_region="us-east-1",
            identity_pool_name="test-pool",
            allowed_bedrock_regions=["us-east-1"],
            monitoring_enabled=True,
            stack_names={"monitoring": "test-otel-collector"},
        )

    def _render_settings(self, otel_resource_attributes=None, settings_version=None):
        """Drive _create_claude_settings with a mocked monitoring stack endpoint."""
        command = PackageCommand()
        profile = self._make_monitoring_profile()

        fake_outputs = json.dumps([{"OutputKey": "CollectorEndpoint", "OutputValue": "https://otel.example.com"}])
        completed = MagicMock(returncode=0, stdout=fake_outputs)

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            with patch(
                "governed_inference_platform.cli.commands.package.subprocess.run",
                return_value=completed,
            ):
                command._create_claude_settings(
                    output_dir,
                    profile,
                    include_coauthored_by=True,
                    profile_name="test",
                    otel_resource_attributes=otel_resource_attributes,
                    settings_version=settings_version,
                )

            settings_path = output_dir / "gip-settings" / "settings.json"
            with open(settings_path, encoding="utf-8") as f:
                return json.load(f)

    def test_default_department_is_default_not_engineering(self):
        """Regression: the default department must be 'default', not 'engineering'.

        'engineering' is not a safe assumption for every deployment, so the
        baseline OTEL attributes should be neutral and overridable.
        """
        settings = self._render_settings()
        attrs = settings["env"]["OTEL_RESOURCE_ATTRIBUTES"]

        assert "department=default" in attrs
        assert "department=engineering" not in attrs

    def test_configured_attributes_override_default(self):
        """An explicit OTEL_RESOURCE_ATTRIBUTES value takes precedence over the default."""
        settings = self._render_settings(otel_resource_attributes="department=research,team.id=ml")
        attrs = settings["env"]["OTEL_RESOURCE_ATTRIBUTES"]

        assert attrs == "department=research,team.id=ml"

    def test_settings_version_appended_to_default_attributes(self):
        """The dist-folder timestamp is stamped as settings_version for telemetry."""
        settings = self._render_settings(settings_version="2026-07-08-120000")
        attrs = settings["env"]["OTEL_RESOURCE_ATTRIBUTES"]

        assert attrs.endswith(",settings_version=2026-07-08-120000")
        assert "department=default" in attrs

    def test_settings_version_appended_to_custom_attributes(self):
        """settings_version is stamped even when the admin customizes attributes."""
        settings = self._render_settings(
            otel_resource_attributes="department=research,team.id=ml",
            settings_version="2026-07-08-120000",
        )
        attrs = settings["env"]["OTEL_RESOURCE_ATTRIBUTES"]

        assert attrs == "department=research,team.id=ml,settings_version=2026-07-08-120000"

    def test_no_settings_version_omits_attribute(self):
        """Without a version (backward compat), the attribute is absent entirely."""
        settings = self._render_settings()
        attrs = settings["env"]["OTEL_RESOURCE_ATTRIBUTES"]

        assert "settings_version" not in attrs

    def test_cowork_package_stamps_settings_version(self):
        """Parity: the CodeBuild package variant stamps settings_version too."""
        from governed_inference_platform.cli.commands.package_cb import PackageCbCommand

        command = PackageCbCommand()
        profile = self._make_monitoring_profile()

        fake_outputs = json.dumps([{"OutputKey": "CollectorEndpoint", "OutputValue": "https://otel.example.com"}])
        completed = MagicMock(returncode=0, stdout=fake_outputs)

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            with patch(
                "governed_inference_platform.cli.commands.package_cb.run_checked",
                return_value=completed,
            ):
                command._create_claude_settings(
                    output_dir,
                    profile,
                    include_coauthored_by=True,
                    profile_name="test",
                    settings_version="2026-07-08-120000",
                )

            settings_path = output_dir / "gip-settings" / "settings.json"
            with open(settings_path, encoding="utf-8") as f:
                settings = json.load(f)

        attrs = settings["env"]["OTEL_RESOURCE_ATTRIBUTES"]
        assert attrs.endswith(",settings_version=2026-07-08-120000")


class TestCopyExtraFiles:
    """Tests for _copy_extra_files — the package-side extra-files copy step."""

    def _profile(self, extra_files):
        return Profile(
            name="test",
            provider_domain="test.okta.com",
            client_id="test-client-id",
            credential_storage="keyring",
            aws_region="us-east-1",
            identity_pool_name="test-pool",
            monitoring_enabled=False,
            extra_files=extra_files,
        )

    def test_empty_list_returns_empty(self, tmp_path):
        command = PackageCommand()
        profile = self._profile([])
        result = command._copy_extra_files(profile, tmp_path, MagicMock())
        assert result == []

    def test_copies_file_and_folder(self, tmp_path):
        # Source file
        src_file = tmp_path / "src" / "preinstall.sh"
        src_file.parent.mkdir(parents=True)
        src_file.write_text("#!/bin/bash\necho hi")
        # Source folder
        src_dir = tmp_path / "src" / "certs"
        src_dir.mkdir()
        (src_dir / "ca.pem").write_text("CERT")

        out = tmp_path / "out"
        out.mkdir()
        profile = self._profile(
            [
                {"name": "preinstall.sh", "targets": "macos", "from": str(src_file)},
                {"name": "certs", "targets": "all", "from": str(src_dir)},
            ]
        )

        result = PackageCommand()._copy_extra_files(profile, out, MagicMock())

        assert (out / "preinstall.sh").read_text() == "#!/bin/bash\necho hi"
        assert (out / "certs" / "ca.pem").read_text() == "CERT"
        names = {name for name, _ in result}
        assert names == {"preinstall.sh", "certs"}

    def test_missing_source_fails(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        profile = self._profile([{"name": "missing.sh", "targets": "all", "from": str(tmp_path / "nope.sh")}])
        result = PackageCommand()._copy_extra_files(profile, out, MagicMock())
        assert result is None
        assert not (out / "missing.sh").exists()

    def test_validation_error_fails_and_copies_nothing(self, tmp_path):
        src = tmp_path / "cfg"
        src.write_text("x")
        out = tmp_path / "out"
        out.mkdir()
        # 'config.json' collides with a generated artifact → validation error
        profile = self._profile([{"name": "config.json", "targets": "all", "from": str(src)}])
        result = PackageCommand()._copy_extra_files(profile, out, MagicMock())
        assert result is None
        assert not (out / "config.json").exists()

    def test_nested_name_creates_parent_dirs(self, tmp_path):
        src = tmp_path / "hook.sh"
        src.write_text("hook")
        out = tmp_path / "out"
        out.mkdir()
        profile = self._profile([{"name": "hooks/pre.sh", "targets": "all", "from": str(src)}])
        result = PackageCommand()._copy_extra_files(profile, out, MagicMock())
        assert result is not None
        assert (out / "hooks" / "pre.sh").read_text() == "hook"

    def test_target_platform_filters_non_matching_extras(self, tmp_path):
        """Regression: --target-platform macos must not copy windows-only extras."""
        for fname in ("hook-win.bat", "hook-mac.sh", "ca.pem"):
            (tmp_path / fname).write_text(fname)
        out = tmp_path / "out"
        out.mkdir()
        profile = self._profile(
            [
                {"name": "ca.pem", "targets": "all", "from": str(tmp_path / "ca.pem")},
                {
                    "name": "hook-mac.sh",
                    "targets": ["macos", "macos-arm64", "macos-intel"],
                    "from": str(tmp_path / "hook-mac.sh"),
                },
                {
                    "name": "hook-win.bat",
                    "targets": ["windows"],
                    "from": str(tmp_path / "hook-win.bat"),
                },
            ]
        )

        result = PackageCommand()._copy_extra_files(profile, out, MagicMock(), ["macos-arm64"])

        assert result is not None
        assert (out / "ca.pem").exists()
        assert (out / "hook-mac.sh").exists()
        assert not (out / "hook-win.bat").exists()
        names = {name for name, _ in result}
        assert names == {"ca.pem", "hook-mac.sh"}

    def test_skipped_extra_does_not_require_source(self, tmp_path):
        """A filtered-out entry's missing 'from' source must not fail the build."""
        out = tmp_path / "out"
        out.mkdir()
        profile = self._profile(
            [{"name": "win-only.bat", "targets": "windows", "from": str(tmp_path / "does-not-exist.bat")}]
        )
        result = PackageCommand()._copy_extra_files(profile, out, MagicMock(), ["macos-arm64"])
        assert result == []

    def test_applicable_extra_missing_source_still_fails(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        profile = self._profile(
            [{"name": "mac-only.sh", "targets": "macos", "from": str(tmp_path / "does-not-exist.sh")}]
        )
        result = PackageCommand()._copy_extra_files(profile, out, MagicMock(), ["macos-arm64"])
        assert result is None

    def test_no_platform_filter_copies_everything(self, tmp_path):
        """platforms_to_build=None keeps the pre-filter superset behavior."""
        (tmp_path / "w.bat").write_text("w")
        out = tmp_path / "out"
        out.mkdir()
        profile = self._profile([{"name": "w.bat", "targets": "windows", "from": str(tmp_path / "w.bat")}])
        result = PackageCommand()._copy_extra_files(profile, out, MagicMock())
        assert result is not None
        assert (out / "w.bat").exists()
