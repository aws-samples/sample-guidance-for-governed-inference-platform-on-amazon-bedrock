# ABOUTME: Unit tests for managed-settings.json deployment feature (#538)
# ABOUTME: Tests Profile backward compat, filename selection, and installer script logic

"""Tests for managed-settings deployment in the package command."""

import json
import tempfile
from pathlib import Path

from governed_inference_platform.cli.commands.package import PackageCommand
from governed_inference_platform.config import Profile


class TestProfileSettingsTarget:
    """Tests for Profile settings_target field and backward compatibility."""

    def test_profile_defaults_to_user_target(self):
        """New profiles default to settings_target='user'."""
        profile = Profile(
            name="test",
            provider_domain="test.okta.com",
            client_id="test-client",
            credential_storage="keyring",
            aws_region="us-east-1",
            identity_pool_name="test-pool",
        )
        assert profile.settings_target == "user"

    def test_profile_accepts_managed_target(self):
        """Profiles can be created with settings_target='managed'."""
        profile = Profile(
            name="test",
            provider_domain="test.okta.com",
            client_id="test-client",
            credential_storage="keyring",
            aws_region="us-east-1",
            identity_pool_name="test-pool",
            settings_target="managed",
        )
        assert profile.settings_target == "managed"

    def test_profile_without_settings_target_field_is_backward_compatible(self):
        """Profiles loaded from old configs without settings_target work correctly."""
        # Simulate loading an old profile dict that lacks the field
        old_profile_data = {
            "name": "legacy",
            "provider_domain": "legacy.okta.com",
            "client_id": "old-client",
            "credential_storage": "keyring",
            "aws_region": "eu-west-1",
            "identity_pool_name": "legacy-pool",
        }
        profile = Profile(**old_profile_data)
        # Should default to "user" — no KeyError or crash
        assert profile.settings_target == "user"

    def test_getattr_settings_target_fallback(self):
        """getattr with default works for settings_target on any profile."""
        profile = Profile(
            name="test",
            provider_domain="test.okta.com",
            client_id="test-client",
            credential_storage="keyring",
            aws_region="us-east-1",
            identity_pool_name="test-pool",
        )
        # This is how the package command reads it
        target = getattr(profile, "settings_target", "user")
        assert target == "user"


class TestGenerateClaudeSettings:
    """Tests for _create_claude_settings filename selection."""

    def _make_profile(self, settings_target="user", monitoring_enabled=False):
        """Helper to create a test profile."""
        return Profile(
            name="test",
            provider_domain="test.okta.com",
            client_id="test-client",
            credential_storage="keyring",
            aws_region="us-east-1",
            identity_pool_name="test-pool",
            monitoring_enabled=monitoring_enabled,
            settings_target=settings_target,
        )

    def test_user_target_creates_settings_json(self):
        """settings_target='user' writes to gip-settings/settings.json."""
        command = PackageCommand()
        profile = self._make_profile(settings_target="user")

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            command._create_claude_settings(output_dir, profile)

            assert (output_dir / "gip-settings" / "settings.json").exists()
            assert not (output_dir / "gip-settings" / "managed-settings.json").exists()

    def test_managed_target_creates_managed_settings_json(self):
        """settings_target='managed' writes to gip-settings/managed-settings.json."""
        command = PackageCommand()
        profile = self._make_profile(settings_target="managed")

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            command._create_claude_settings(output_dir, profile)

            assert (output_dir / "gip-settings" / "managed-settings.json").exists()
            assert not (output_dir / "gip-settings" / "settings.json").exists()

    def test_managed_settings_contains_correct_content(self):
        """managed-settings.json has the same Bedrock config as settings.json would."""
        command = PackageCommand()
        profile = self._make_profile(settings_target="managed")

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            command._create_claude_settings(output_dir, profile)

            settings_path = output_dir / "gip-settings" / "managed-settings.json"
            with open(settings_path) as f:
                settings = json.load(f)

            # Must contain Bedrock env vars
            assert settings["env"]["CLAUDE_CODE_USE_BEDROCK"] == "1"
            assert settings["env"]["AWS_REGION"] == "us-east-1"
            assert "__CREDENTIAL_PROCESS_PATH__" in settings["env"]["AWS_CREDENTIAL_PROCESS"]

    def test_user_target_settings_content_unchanged(self):
        """Default user target still produces correct settings.json content."""
        command = PackageCommand()
        profile = self._make_profile(settings_target="user")

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            command._create_claude_settings(output_dir, profile)

            settings_path = output_dir / "gip-settings" / "settings.json"
            with open(settings_path) as f:
                settings = json.load(f)

            assert settings["env"]["CLAUDE_CODE_USE_BEDROCK"] == "1"
            assert settings["env"]["AWS_REGION"] == "us-east-1"


class TestManagedSettingsGatewayIsolation:
    """Legacy gateway fields never synthesize upstream-managed client policy."""

    def _make_profile(
        self,
        settings_target="managed",
        gateway_enabled=True,
        gateway_public_url="https://claude-gateway.internal.example.com",
    ):
        return Profile(
            name="test",
            provider_domain="test.okta.com",
            client_id="test-client",
            credential_storage="keyring",
            aws_region="us-east-1",
            identity_pool_name="test-pool",
            monitoring_enabled=False,
            settings_target=settings_target,
            gateway_enabled=gateway_enabled,
            gateway_public_url=gateway_public_url,
        )

    def _generate_settings(self, profile):
        command = PackageCommand()
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            command._create_claude_settings(output_dir, profile)
            filename = "managed-settings.json" if profile.settings_target == "managed" else "settings.json"
            with open(output_dir / "gip-settings" / filename) as f:
                return json.load(f)

    def test_managed_target_with_legacy_gateway_has_no_force_login_keys(self):
        settings = self._generate_settings(self._make_profile())
        assert "forceLoginMethod" not in settings
        assert "forceLoginGatewayUrl" not in settings

    def test_user_target_never_gets_force_login_keys(self):
        settings = self._generate_settings(self._make_profile(settings_target="user"))
        assert "forceLoginMethod" not in settings
        assert "forceLoginGatewayUrl" not in settings


class TestInstallerScriptManagedSettings:
    """Tests for install.sh template handling of managed-settings.json."""

    def _get_installer_content(self, profile):
        """Generate installer and return its content."""
        command = PackageCommand()

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)

            # Create minimal required structure
            (output_dir / "gip-settings").mkdir()
            (output_dir / "config.json").write_text("{}")

            # Generate settings based on profile target
            command._create_claude_settings(output_dir, profile)

            # Build a mock executables list
            built_executables = [("darwin-arm64", output_dir / "credential-process")]
            (output_dir / "credential-process").touch()

            # Generate installer
            installer_path = command._create_installer(output_dir, profile, built_executables, built_otel_helpers=[])

            return installer_path.read_text(encoding="utf-8")

    def test_managed_installer_contains_elevation_check(self):
        """Installer checks for root when managed-settings.json is present."""
        profile = Profile(
            name="test",
            provider_domain="test.okta.com",
            client_id="test-client",
            credential_storage="keyring",
            aws_region="us-east-1",
            identity_pool_name="test-pool",
            settings_target="managed",
            monitoring_enabled=False,
        )

        content = self._get_installer_content(profile)

        # Should contain managed-settings handling
        assert "managed-settings.json" in content
        assert "id -u" in content or "sudo" in content

    def test_user_installer_preserves_foreign_settings(self):
        """User-mode installer does not mutate a settings file it cannot prove it owns."""
        profile = Profile(
            name="test",
            provider_domain="test.okta.com",
            client_id="test-client",
            credential_storage="keyring",
            aws_region="us-east-1",
            identity_pool_name="test-pool",
            settings_target="user",
            monitoring_enabled=False,
        )

        content = self._get_installer_content(profile)

        assert "Preserving existing foreign Claude Code settings" in content
        assert "deep_merge" not in content

    def test_user_installer_uses_atomic_owned_write(self):
        """Owned or absent settings are installed through the no-follow helper."""
        profile = Profile(
            name="test",
            provider_domain="test.okta.com",
            client_id="test-client",
            credential_storage="keyring",
            aws_region="us-east-1",
            identity_pool_name="test-pool",
            settings_target="user",
            monitoring_enabled=False,
        )

        content = self._get_installer_content(profile)

        assert 'gip_atomic_copy "$SETTINGS_SOURCE" ".claude/settings.json"' in content

    def test_user_installer_does_not_claim_merged_settings(self):
        profile = Profile(
            name="test",
            provider_domain="test.okta.com",
            client_id="test-client",
            credential_storage="keyring",
            aws_region="us-east-1",
            identity_pool_name="test-pool",
            settings_target="user",
            monitoring_enabled=False,
        )

        content = self._get_installer_content(profile)

        assert 'GIP_SETTINGS_CREATED="$GIP_SETTINGS_WAS_OWNED"' in content
        assert "GIP_SETTINGS_CREATED=1" in content
        assert 'os.environ.get("GIP_SETTINGS_CREATED") == "1"' in content


class TestInstallerMergeLogic:
    """Tests for the deep merge logic in the installer."""

    def test_deep_merge_preserves_user_keys(self):
        """The merge function preserves keys that only exist in the user's config."""

        def deep_merge(base, override):
            result = base.copy()
            for key, value in override.items():
                if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                    result[key] = deep_merge(result[key], value)
                else:
                    result[key] = value
            return result

        existing = {
            "env": {"MY_CUSTOM_VAR": "keep_me", "AWS_REGION": "old-region"},
            "myCustomSetting": True,
        }
        incoming = {
            "env": {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_REGION": "us-east-1"},
        }

        merged = deep_merge(existing, incoming)

        # User's custom key preserved
        assert merged["myCustomSetting"] is True
        # User's env var preserved
        assert merged["env"]["MY_CUSTOM_VAR"] == "keep_me"
        # Incoming values applied
        assert merged["env"]["CLAUDE_CODE_USE_BEDROCK"] == "1"
        # Conflicting key overridden by incoming
        assert merged["env"]["AWS_REGION"] == "us-east-1"

    def test_deep_merge_handles_nested_dicts(self):
        """Deep merge recurses into nested dictionaries."""

        def deep_merge(base, override):
            result = base.copy()
            for key, value in override.items():
                if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                    result[key] = deep_merge(result[key], value)
                else:
                    result[key] = value
            return result

        existing = {"env": {"A": "1", "B": "2"}, "permissions": {"deny": ["rm"]}}
        incoming = {"env": {"C": "3"}, "permissions": {"deny": ["rm", "sudo"]}}

        merged = deep_merge(existing, incoming)

        assert merged["env"] == {"A": "1", "B": "2", "C": "3"}
        # Non-dict values are overwritten (not merged)
        assert merged["permissions"]["deny"] == ["rm", "sudo"]


class TestInstallerSudoOwnership:
    """Regression tests: user-scoped installation rejects whole-script elevation."""

    def _get_installer_script(self, profile, settings_target="user"):
        """Generate an install.sh string for the given profile."""
        profile.settings_target = settings_target
        cmd = PackageCommand()
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            # create dummy files so the method doesn't fail on missing artefacts
            (output_dir / "gip-settings").mkdir()
            if settings_target == "managed":
                (output_dir / "gip-settings" / "managed-settings.json").write_text("{}")
            else:
                (output_dir / "gip-settings" / "settings.json").write_text("{}")
            installer = cmd._create_installer(output_dir, profile, [], [])
            return installer.read_text(encoding="utf-8")

    def _make_profile(self):
        return Profile(
            name="Test",
            provider_domain="test.okta.com",
            client_id="client-id",
            credential_storage="keyring",
            aws_region="us-east-1",
            identity_pool_name="test-pool",
        )

    def test_installer_rejects_whole_script_sudo(self):
        """User-scoped writes must never run through a root-owned installer process."""
        script = self._get_installer_script(self._make_profile())
        assert "Do not run this installer with sudo or as root" in script
        assert '[ "$(id -u)" -eq 0 ]' in script

    def test_installer_falls_back_to_home_when_no_sudo(self):
        """install.sh must fall back to $HOME/$USER when SUDO_USER is not set."""
        script = self._get_installer_script(self._make_profile())
        assert 'ACTUAL_HOME="$HOME"' in script
        assert 'ACTUAL_USER="$USER"' in script

    def test_installer_user_files_use_actual_home(self):
        """User-space paths (~/gip, ~/.claude, ~/.aws) must use ACTUAL_HOME."""
        script = self._get_installer_script(self._make_profile())
        # Must not hard-code ~ for user-space directories
        assert "mkdir -p ~/gip" not in script
        assert "mkdir -p ~/.claude" not in script
        assert "mkdir -p ~/.aws" not in script
        # Must use ACTUAL_HOME variable instead
        assert "$ACTUAL_HOME/gip" in script
        assert "$ACTUAL_HOME/.claude" in script
        assert "$ACTUAL_HOME/.aws" in script

    def test_installer_managed_settings_does_not_exit_if_not_root(self):
        """Managed settings must not exit 1 when script runs as non-root; use inline sudo instead."""
        profile = self._make_profile()
        script = self._get_installer_script(profile, settings_target="managed")
        # Old bad pattern must be gone
        assert "Re-run the installer with: sudo ./install.sh" not in script
        # New inline-sudo pattern must be present
        assert "sudo mkdir" in script or "sudo tee" in script

    def test_installer_managed_settings_still_elevates_narrowly(self):
        """Only the managed-settings write may elevate after whole-script sudo is rejected."""
        assert "sudo mkdir -p" in self._get_installer_script(self._make_profile(), settings_target="managed")

    def test_managed_settings_conflict_is_preflighted_and_owned_upgrades_are_marked(self):
        script = self._get_installer_script(self._make_profile(), settings_target="managed")

        assert script.index("managed_marker = managed_dir") < script.index("Installing authentication tools")
        assert ".managed-settings.gip-sha256" in script
        assert "Preserving existing foreign or changed managed settings" in script
