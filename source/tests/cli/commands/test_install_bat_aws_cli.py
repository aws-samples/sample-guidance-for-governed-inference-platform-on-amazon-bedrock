# ABOUTME: Tests that install.bat configures AWS profiles without depending on AWS CLI
# ABOUTME: Regression test for issue #592 (false success when aws not found)

"""Tests for install.bat AWS CLI handling logic."""

from pathlib import Path

import pytest


class TestInstallBatAwsCliHandling:
    """Verify install.bat correctly handles AWS CLI presence/absence."""

    @pytest.fixture(autouse=True)
    def load_package_py(self):
        self.package_path = (
            Path(__file__).parent.parent.parent.parent
            / "governed_inference_platform"
            / "cli"
            / "commands"
            / "package.py"
        )
        self.content = self.package_path.read_text(encoding="utf-8")

    def test_aws_cli_is_optional(self):
        assert "AWS CLI not found -- not required" in self.content

    def test_profile_config_uses_transaction_helper(self):
        assert 'gip-install.ps1" -InstallAll' in self.content

    def test_fallback_writes_config_directly(self):
        """When AWS CLI is absent, must write ~/.aws/config directly via PowerShell."""
        assert ".aws" in self.content
        assert "config" in self.content
        assert "[profile" in self.content
        assert "credential_process" in self.content

    def test_profile_setup_does_not_call_aws_configure(self):
        # Find the profile configuration section
        profile_section_start = self.content.find("REM Configure AWS profiles")
        profile_section_end = self.content.find("echo Installation complete", profile_section_start)
        profile_section = self.content[profile_section_start:profile_section_end]

        assert "aws configure" not in profile_section

    def test_success_message_only_on_actual_success(self):
        """'OK Created AWS profile' must not be printed unconditionally after aws calls."""
        profile_section_start = self.content.find("REM Configure AWS profiles")
        profile_section_end = self.content.find("echo Installation complete", profile_section_start)
        profile_section = self.content[profile_section_start:profile_section_end]

        # The old bug: 'echo OK Created' appeared outside any error check
        lines = profile_section.splitlines()
        for i, line in enumerate(lines):
            if "OK Created AWS profile" in line:
                # Must be inside a conditional (errorlevel check or PowerShell Write-Host)
                context = "\n".join(lines[max(0, i - 3) : i + 1])
                is_conditional = "errorlevel" in context.lower() or "else" in context or "Write-Host" in line
                assert is_conditional, f"'OK Created AWS profile' at line {i} must be inside an error-check branch"
