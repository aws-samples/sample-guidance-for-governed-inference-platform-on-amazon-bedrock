# ABOUTME: Tests that gip package produces complete output for each profile type.
# ABOUTME: Verifies the wiring of build steps by mocking subprocess and file I/O,
# ABOUTME: ensuring each profile type produces the expected manifest of output files.

"""Tests that gip package produces complete output for each profile type.

For a sidecar OIDC profile:
  - output should contain collector-config.yaml
  - output should reference otelcol build
  - output should reference credential-process binary

For a central monitoring profile:
  - output should NOT contain collector-config.yaml or otelcol

For IDC zero-binary:
  - output should NOT contain any Go binaries
  - output should contain collector-config.yaml (IDC template)
"""

import hashlib
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from governed_inference_platform.cli.commands.cleanup import CleanupCommand
from governed_inference_platform.cli.commands.package import PackageCommand
from governed_inference_platform.config import Profile

# ---------------------------------------------------------------------------
# Fixtures: profile factories
# ---------------------------------------------------------------------------


def _make_oidc_sidecar_profile():
    """OIDC profile with sidecar monitoring — requires all binaries + collector."""
    return Profile(
        name="test-oidc-sidecar",
        provider_domain="auth.example.com",
        client_id="client-abc",
        credential_storage="keyring",
        aws_region="us-east-1",
        identity_pool_name="test-pool",
        auth_type="oidc",
        monitoring_enabled=True,
        monitoring_mode="sidecar",
        otel_collector_endpoint="https://alb.example.com",
    )


def _make_oidc_central_profile():
    """OIDC profile with central monitoring — no collector config or otelcol binary."""
    return Profile(
        name="test-oidc-central",
        provider_domain="auth.example.com",
        client_id="client-abc",
        credential_storage="keyring",
        aws_region="us-east-1",
        identity_pool_name="test-pool",
        auth_type="oidc",
        monitoring_enabled=True,
        monitoring_mode="central",
        otel_collector_endpoint="https://alb.example.com",
    )


def _make_idc_zero_binary_profile():
    """IDC profile without quota — zero-binary mode, no Go builds."""
    return Profile(
        name="test-idc-zero",
        provider_domain="",
        client_id="",
        credential_storage="keyring",
        aws_region="us-east-1",
        identity_pool_name="",
        auth_type="idc",
        idc_start_url="https://example.awsapps.com/start",
        idc_account_id="123456789012",
        idc_permission_set_name="BedrockDeveloperAccess",
        monitoring_enabled=True,
        monitoring_mode="sidecar",
        # No quota_api_endpoint → zero-binary mode
    )


# ---------------------------------------------------------------------------
# Helper: run the package command's post-build phase (config + collector config)
# ---------------------------------------------------------------------------


def _run_config_phase(profile, output_dir, built_executables=None, built_otel_helpers=None):
    """Run the configuration-generation phase of PackageCommand.

    This exercises _generate_collector_config, _create_config, _create_installer,
    _create_claude_settings without actually invoking subprocess for Go builds.
    """
    cmd = PackageCommand()

    # _create_config needs a federation identifier and type
    federation_identifier = "arn:aws:iam::123456789012:role/test-role"
    federation_type = "direct"

    # Create config.json
    cmd._create_config(output_dir, profile, federation_identifier, federation_type, profile.name, MagicMock())

    # Generate collector config if sidecar
    _is_sidecar = getattr(profile, "monitoring_mode", "central") == "sidecar"
    _is_idc_auth = getattr(profile, "effective_auth_type", profile.auth_type) == "idc"

    if profile.monitoring_enabled and _is_sidecar:
        if _is_idc_auth:
            cmd._generate_collector_config(
                output_dir=output_dir,
                template_name="collector-config-idc.yaml",
                region=profile.aws_region or "us-east-1",
                idc_user_email="test@example.com",
            )
        else:
            cmd._generate_collector_config(
                output_dir=output_dir,
                template_name="collector-config.yaml",
                region=profile.aws_region or "us-east-1",
            )

    # Create installer
    if built_executables is None:
        built_executables = []
    if built_otel_helpers is None:
        built_otel_helpers = []

    # Ensure installer prerequisites exist
    (output_dir / "gip-settings").mkdir(exist_ok=True)
    cmd._create_installer(output_dir, profile, built_executables, built_otel_helpers)

    # Create Claude settings
    cmd._create_claude_settings(output_dir, profile)


# ---------------------------------------------------------------------------
# Tests: OIDC Sidecar Profile Manifest
# ---------------------------------------------------------------------------


class TestOIDCSidecarManifest:
    """OIDC sidecar profile must produce collector-config.yaml and reference otelcol + credential-process."""

    def test_collector_config_present(self, tmp_path):
        """Sidecar OIDC must produce collector-config.yaml."""
        profile = _make_oidc_sidecar_profile()
        _run_config_phase(profile, tmp_path)
        assert (tmp_path / "collector-config.yaml").exists(), (
            "collector-config.yaml missing from OIDC sidecar package — local collector will have no configuration."
        )

    def test_collector_config_has_region(self, tmp_path):
        """collector-config.yaml must have the region substituted (no ${REGION} placeholders)."""
        profile = _make_oidc_sidecar_profile()
        _run_config_phase(profile, tmp_path)
        content = (tmp_path / "collector-config.yaml").read_text(encoding="utf-8")
        assert "${REGION}" not in content
        assert "us-east-1" in content

    def test_installer_references_otelcol(self, tmp_path):
        """Installer must reference otelcol binary for sidecar deployment."""
        profile = _make_oidc_sidecar_profile()
        # Create a dummy executable so installer generation succeeds
        dummy_exec = tmp_path / "credential-process-macos-arm64"
        dummy_exec.touch()
        built_executables = [("macos-arm64", dummy_exec)]
        _run_config_phase(profile, tmp_path, built_executables=built_executables)

        installer = tmp_path / "install.sh"
        assert installer.exists(), "install.sh not generated"
        content = installer.read_text(encoding="utf-8")
        assert "otelcol" in content, "Installer does not reference otelcol — sidecar collector will not be installed."

    def test_installer_references_credential_process(self, tmp_path):
        """Installer must reference credential-process binary."""
        profile = _make_oidc_sidecar_profile()
        dummy_exec = tmp_path / "credential-process-macos-arm64"
        dummy_exec.touch()
        built_executables = [("macos-arm64", dummy_exec)]
        _run_config_phase(profile, tmp_path, built_executables=built_executables)

        installer = tmp_path / "install.sh"
        assert installer.exists()
        content = installer.read_text(encoding="utf-8")
        assert "credential-process" in content, "Installer does not reference credential-process binary."

    def test_claude_settings_has_otel_endpoint(self, tmp_path):
        """Claude settings must configure OTEL endpoint for sidecar (localhost:4318)."""
        profile = _make_oidc_sidecar_profile()
        _run_config_phase(profile, tmp_path)

        settings_path = tmp_path / "gip-settings" / "settings.json"
        assert settings_path.exists()
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        assert settings["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://localhost:4318"


# ---------------------------------------------------------------------------
# Tests: OIDC Central Profile Manifest
# ---------------------------------------------------------------------------


class TestOIDCCentralManifest:
    """OIDC central profile must NOT produce collector-config.yaml or reference otelcol."""

    def test_no_collector_config(self, tmp_path):
        """Central mode must NOT produce collector-config.yaml (collector runs on ECS)."""
        profile = _make_oidc_central_profile()
        _run_config_phase(profile, tmp_path)
        assert not (tmp_path / "collector-config.yaml").exists(), (
            "collector-config.yaml should NOT exist for central monitoring — "
            "it would confuse the installer into deploying a local collector."
        )

    def test_no_otelcol_in_output(self, tmp_path):
        """Central mode should not ship otelcol binaries."""
        profile = _make_oidc_central_profile()
        _run_config_phase(profile, tmp_path)
        otelcol_files = list(tmp_path.glob("otelcol-*"))
        assert len(otelcol_files) == 0, f"Unexpected otelcol binaries in central mode package: {otelcol_files}"

    def test_installer_still_valid(self, tmp_path):
        """Central mode must still produce a usable installer."""
        profile = _make_oidc_central_profile()
        dummy_exec = tmp_path / "credential-process-macos-arm64"
        dummy_exec.touch()
        built_executables = [("macos-arm64", dummy_exec)]
        _run_config_phase(profile, tmp_path, built_executables=built_executables)

        installer = tmp_path / "install.sh"
        assert installer.exists()
        content = installer.read_text(encoding="utf-8")
        # Central mode installer should NOT try to install a local collector
        assert "credential-process" in content


# ---------------------------------------------------------------------------
# Tests: IDC Zero-Binary Manifest
# ---------------------------------------------------------------------------


class TestIDCZeroBinaryManifest:
    """IDC zero-binary mode must NOT include Go binaries but MUST include collector-config.yaml."""

    def test_no_go_binaries_in_output(self, tmp_path):
        """IDC zero-binary must not produce credential-process or otel-helper binaries."""
        profile = _make_idc_zero_binary_profile()
        _run_config_phase(profile, tmp_path)
        # No credential-process-* or otel-helper-* files should exist
        cred_binaries = list(tmp_path.glob("credential-process-*"))
        otel_binaries = list(tmp_path.glob("otel-helper-*"))
        assert len(cred_binaries) == 0, f"credential-process binaries found in IDC zero-binary package: {cred_binaries}"
        assert len(otel_binaries) == 0, f"otel-helper binaries found in IDC zero-binary package: {otel_binaries}"

    def test_collector_config_present_with_idc_template(self, tmp_path):
        """IDC zero-binary sidecar MUST produce collector-config.yaml with static identity."""
        profile = _make_idc_zero_binary_profile()
        _run_config_phase(profile, tmp_path)
        assert (tmp_path / "collector-config.yaml").exists(), (
            "collector-config.yaml missing from IDC zero-binary sidecar package."
        )

    def test_collector_config_has_static_identity(self, tmp_path):
        """IDC collector config must bake in the user email (no runtime otel-helper)."""
        profile = _make_idc_zero_binary_profile()
        _run_config_phase(profile, tmp_path)
        content = (tmp_path / "collector-config.yaml").read_text(encoding="utf-8")
        assert "test@example.com" in content, "IDC collector-config.yaml does not contain baked-in user email."
        assert "${USER_EMAIL}" not in content, (
            "IDC collector-config.yaml still has ${USER_EMAIL} placeholder — identity was not substituted."
        )

    def test_no_otelcol_binaries(self, tmp_path):
        """IDC zero-binary must NOT include otelcol binaries (no Go toolchain assumed)."""
        profile = _make_idc_zero_binary_profile()
        _run_config_phase(profile, tmp_path)
        otelcol_files = list(tmp_path.glob("otelcol-*"))
        assert len(otelcol_files) == 0, (
            f"otelcol binaries found in IDC zero-binary package: {otelcol_files} — "
            "IDC zero-binary contract assumes no build tools on admin machine."
        )

    def test_installer_records_files_profile_and_sso_session(self, tmp_path):
        profile = _make_idc_zero_binary_profile()
        _run_config_phase(profile, tmp_path)

        installer = (tmp_path / "install.sh").read_text(encoding="utf-8")

        assert ".gip-ownership.json" in installer
        assert '"aws_profiles"' in installer
        assert '"aws_sso_sessions"' in installer
        assert "GIP_SETTINGS_CREATED" in installer
        assert "Preserving existing Claude Code settings" in installer

    def test_installer_and_cleanup_round_trip_owned_state(self, tmp_path, monkeypatch):
        package_dir = tmp_path / "package"
        package_dir.mkdir()
        profile = _make_idc_zero_binary_profile()
        _run_config_phase(profile, package_dir)

        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        fake_aws = fake_bin / "aws"
        fake_aws.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_aws.chmod(0o755)
        home = tmp_path / "home"
        home.mkdir()
        env = {
            **os.environ,
            "HOME": str(home),
            "USER": "gip-test-user",
            "SUDO_USER": "",
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        }

        result = subprocess.run(  # nosec B603 -- fixed argv, test-controlled input
            ["bash", "install.sh"],
            cwd=package_dir,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr or result.stdout
        manifest_path = home / "gip" / ".gip-ownership.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert set(manifest["aws_profiles"]) == {"gip"}
        assert set(manifest["aws_sso_sessions"]) == {"gip-session"}
        assert ".gip/collector-config.yaml" in manifest["files"]
        assert ".claude/settings.json" in manifest["files"]

        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        command = CleanupCommand.__new__(CleanupCommand)
        command.option = {"profile": "gip", "force": True, "credentials-only": False}.get

        assert command.handle() == 0
        assert not (home / "gip").exists()
        assert not (home / ".gip" / "collector-config.yaml").exists()
        assert not (home / ".claude" / "settings.json").exists()
        aws_config = (home / ".aws" / "config").read_text(encoding="utf-8")
        assert "gip" not in aws_config

    def test_existing_settings_are_preserved_and_not_claimed(self, tmp_path):
        package_dir = tmp_path / "package"
        package_dir.mkdir()
        profile = _make_idc_zero_binary_profile()
        _run_config_phase(profile, package_dir)

        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        fake_aws = fake_bin / "aws"
        fake_aws.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_aws.chmod(0o755)
        home = tmp_path / "home"
        settings = home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text('{"userSetting": true}\n', encoding="utf-8")
        env = {
            **os.environ,
            "HOME": str(home),
            "USER": "gip-test-user",
            "SUDO_USER": "",
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        }

        result = subprocess.run(  # nosec B603 -- fixed argv, test-controlled input
            ["bash", "install.sh"], cwd=package_dir, env=env, capture_output=True, text=True, check=False
        )

        assert result.returncode == 0, result.stderr or result.stdout
        assert settings.read_text(encoding="utf-8") == '{"userSetting": true}\n'
        manifest = json.loads((home / "gip" / ".gip-ownership.json").read_text(encoding="utf-8"))
        assert ".claude/settings.json" not in manifest["files"]

    def test_existing_aws_profile_fails_before_side_effects(self, tmp_path):
        package_dir = tmp_path / "package"
        package_dir.mkdir()
        profile = _make_idc_zero_binary_profile()
        _run_config_phase(profile, package_dir)

        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        fake_aws = fake_bin / "aws"
        fake_aws.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_aws.chmod(0o755)
        home = tmp_path / "home"
        aws_config = home / ".aws" / "config"
        aws_config.parent.mkdir(parents=True)
        aws_config.write_text("[profile gip]\nregion = us-west-2\n", encoding="utf-8")
        env = {
            **os.environ,
            "HOME": str(home),
            "USER": "gip-test-user",
            "SUDO_USER": "",
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        }

        result = subprocess.run(  # nosec B603 -- fixed argv, test-controlled input
            ["bash", "install.sh"], cwd=package_dir, env=env, capture_output=True, text=True, check=False
        )

        assert result.returncode != 0
        assert "foreign AWS profile" in result.stdout
        assert not (home / "gip").exists()

    def test_reinstall_accepts_owned_unchanged_and_rejects_owned_modified(self, tmp_path):
        package_dir = tmp_path / "package"
        package_dir.mkdir()
        _run_config_phase(_make_idc_zero_binary_profile(), package_dir)
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        fake_aws = fake_bin / "aws"
        fake_aws.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_aws.chmod(0o755)
        home = tmp_path / "home"
        home.mkdir()
        env = {
            **os.environ,
            "HOME": str(home),
            "USER": "gip-test-user",
            "SUDO_USER": "",
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        }

        first = subprocess.run(  # nosec B603 -- fixed argv, test-controlled input
            ["bash", "install.sh"], cwd=package_dir, env=env, capture_output=True, text=True, check=False
        )
        second = subprocess.run(  # nosec B603 -- fixed argv, test-controlled input
            ["bash", "install.sh"], cwd=package_dir, env=env, capture_output=True, text=True, check=False
        )

        assert first.returncode == 0, first.stderr or first.stdout
        assert second.returncode == 0, second.stderr or second.stdout

        config = home / "gip" / "config.json"
        config.write_text('{"modified": true}\n', encoding="utf-8")
        modified = subprocess.run(  # nosec B603 -- fixed argv, test-controlled input
            ["bash", "install.sh"], cwd=package_dir, env=env, capture_output=True, text=True, check=False
        )

        assert modified.returncode != 0
        assert "owned-modified target" in modified.stdout
        assert config.read_text(encoding="utf-8") == '{"modified": true}\n'


def test_external_installers_without_manifest_support_fail_closed(tmp_path):
    (tmp_path / "install.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "gip-install.ps1").write_text("Write-Host 'install'\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unverified external installer"):
        PackageCommand()._create_installer(tmp_path, _make_oidc_central_profile(), [], [])


def test_single_external_installer_also_fails_closed(tmp_path):
    (tmp_path / "install.sh").write_text("# .gip-ownership.json marker is not proof\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unverified external installer"):
        PackageCommand()._create_installer(tmp_path, _make_oidc_central_profile(), [], [])


def test_standard_installer_rejects_foreign_managed_file_before_writes(tmp_path):
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    profile = _make_oidc_central_profile()
    suffix = "macos-arm64" if os.uname().machine == "arm64" else "macos-intel"
    binary = package_dir / f"credential-process-{suffix}"
    binary.write_text("new-binary", encoding="utf-8")
    _run_config_phase(profile, package_dir, built_executables=[(suffix, binary)])

    home = tmp_path / "home"
    install = home / "gip"
    install.mkdir(parents=True)
    target = install / "credential-process"
    target.write_text("foreign-binary", encoding="utf-8")
    env = {**os.environ, "HOME": str(home), "USER": "gip-test-user", "SUDO_USER": ""}

    result = subprocess.run(  # nosec B603 -- fixed argv, test-controlled input
        ["bash", "install.sh"], cwd=package_dir, env=env, capture_output=True, text=True, check=False
    )

    assert result.returncode != 0
    assert "foreign target" in result.stderr
    assert target.read_text(encoding="utf-8") == "foreign-binary"
    assert not (home / ".aws" / "config").exists()


def test_standard_installer_reinstall_accepts_owned_unchanged_state(tmp_path):
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    profile = _make_oidc_central_profile()
    suffix = "macos-arm64" if os.uname().machine == "arm64" else "macos-intel"
    binary = package_dir / f"credential-process-{suffix}"
    binary.write_text("binary", encoding="utf-8")
    binary.chmod(0o755)
    _run_config_phase(profile, package_dir, built_executables=[(suffix, binary)])

    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "USER": "gip-test-user", "SUDO_USER": ""}

    first = subprocess.run(  # nosec B603 -- fixed argv, test-controlled input
        ["bash", "install.sh"], cwd=package_dir, env=env, capture_output=True, text=True, check=False
    )
    second = subprocess.run(  # nosec B603 -- fixed argv, test-controlled input
        ["bash", "install.sh"], cwd=package_dir, env=env, capture_output=True, text=True, check=False
    )

    assert first.returncode == 0, first.stderr or first.stdout
    assert second.returncode == 0, second.stderr or second.stdout


def test_standard_installer_reinstalls_all_resolved_managed_files_and_rejects_tampering(tmp_path):
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    profile = _make_oidc_central_profile()
    profile.web_search_enabled = True
    profile.websearch_gateway_url = "https://gateway.example.com/mcp"
    suffix = "macos-arm64" if os.uname().machine == "arm64" else "macos-intel"
    binary = package_dir / f"credential-process-{suffix}"
    binary.write_text("binary", encoding="utf-8")
    binary.chmod(0o755)
    _run_config_phase(profile, package_dir, built_executables=[(suffix, binary)])
    PackageCommand()._create_harness_configs(package_dir, profile, profile.name, ["opencode", "codex"], MagicMock())
    (package_dir / "cowork-3p.mobileconfig").write_text("home=__GIP_HOME__\n", encoding="utf-8")
    (package_dir / "cowork-3p-config.json").write_text('{"home": "__GIP_HOME__"}\n', encoding="utf-8")
    mcp_source = package_dir / "gip-settings" / "mcp.json"
    mcp_source.write_text('{"headersHelper": "__WEBSEARCH_HEADERS_HELPER__"}\n', encoding="utf-8")

    harness_sources = sorted(path for path in (package_dir / "harnesses").rglob("*") if path.is_file())
    sources = [
        *harness_sources,
        package_dir / "cowork-3p.mobileconfig",
        package_dir / "cowork-3p-config.json",
        mcp_source,
    ]
    fingerprints = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "USER": "gip-test-user", "SUDO_USER": ""}

    first = subprocess.run(  # nosec B603 -- fixed argv, test-controlled input
        ["bash", "install.sh"], cwd=package_dir, env=env, capture_output=True, text=True, check=False
    )
    second = subprocess.run(  # nosec B603 -- fixed argv, test-controlled input
        ["bash", "install.sh"], cwd=package_dir, env=env, capture_output=True, text=True, check=False
    )

    assert first.returncode == 0, first.stderr or first.stdout
    assert second.returncode == 0, second.stderr or second.stdout
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in sources} == fingerprints
    install = home / "gip"
    installed = install / "harnesses"
    assert "__CREDENTIAL_PROCESS_PATH__" not in (installed / "opencode" / "opencode.json").read_text()
    assert (installed / "codex" / "config.toml").is_file()
    assert "__GIP_HOME__" not in (install / "cowork-3p.mobileconfig").read_text()
    assert "__GIP_HOME__" not in (install / "cowork-3p-config.json").read_text()
    assert "__WEBSEARCH_HEADERS_HELPER__" not in (install / "gip-settings" / "mcp.json").read_text()
    manifest = json.loads((home / "gip" / ".gip-ownership.json").read_text(encoding="utf-8"))
    managed = {
        "gip/cowork-3p.mobileconfig",
        "gip/cowork-3p-config.json",
        "gip/gip-settings/mcp.json",
    }
    managed.update(
        f"gip/harnesses/{path.relative_to(package_dir / 'harnesses').as_posix()}" for path in harness_sources
    )
    assert managed <= set(manifest["files"])
    for key in managed:
        assert manifest["files"][key] == hashlib.sha256((home / key).read_bytes()).hexdigest()
    assert "test-oidc-central" in manifest["aws_profiles"]

    tampered = installed / "opencode" / "opencode.json"
    tampered.write_text('{"tampered": true}\n', encoding="utf-8")
    rejected = subprocess.run(  # nosec B603 -- fixed argv, test-controlled input
        ["bash", "install.sh"], cwd=package_dir, env=env, capture_output=True, text=True, check=False
    )

    assert rejected.returncode != 0
    assert "owned-modified target" in rejected.stderr
    assert tampered.read_text(encoding="utf-8") == '{"tampered": true}\n'


def test_standard_installer_rejects_foreign_resolved_managed_file_before_writes(tmp_path):
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    profile = _make_oidc_central_profile()
    suffix = "macos-arm64" if os.uname().machine == "arm64" else "macos-intel"
    binary = package_dir / f"credential-process-{suffix}"
    binary.write_text("new-binary", encoding="utf-8")
    binary.chmod(0o755)
    _run_config_phase(profile, package_dir, built_executables=[(suffix, binary)])
    harness_source = package_dir / "harnesses" / "opencode" / "opencode.json"
    harness_source.parent.mkdir(parents=True)
    harness_source.write_text('{"managed": true}\n', encoding="utf-8")

    home = tmp_path / "home"
    foreign = home / "gip" / "harnesses" / "opencode" / "opencode.json"
    foreign.parent.mkdir(parents=True)
    foreign.write_text('{"foreign": true}\n', encoding="utf-8")
    env = {**os.environ, "HOME": str(home), "USER": "gip-test-user", "SUDO_USER": ""}

    result = subprocess.run(  # nosec B603 -- fixed argv, test-controlled input
        ["bash", "install.sh"], cwd=package_dir, env=env, capture_output=True, text=True, check=False
    )

    assert result.returncode != 0
    assert "foreign target" in result.stderr
    assert foreign.read_text(encoding="utf-8") == '{"foreign": true}\n'
    assert not (home / "gip" / "credential-process").exists()


def test_standard_installer_preserves_foreign_settings_and_does_not_claim_them(tmp_path):
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    profile = _make_oidc_central_profile()
    suffix = "macos-arm64" if os.uname().machine == "arm64" else "macos-intel"
    binary = package_dir / f"credential-process-{suffix}"
    binary.write_text("binary", encoding="utf-8")
    binary.chmod(0o755)
    _run_config_phase(profile, package_dir, built_executables=[(suffix, binary)])
    home = tmp_path / "home"
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"userSetting": true}\n', encoding="utf-8")
    env = {**os.environ, "HOME": str(home), "USER": "gip-test-user", "SUDO_USER": ""}

    result = subprocess.run(  # nosec B603 -- fixed argv, test-controlled input
        ["bash", "install.sh"], cwd=package_dir, env=env, capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert settings.read_text(encoding="utf-8") == '{"userSetting": true}\n'
    manifest = json.loads((home / "gip" / ".gip-ownership.json").read_text(encoding="utf-8"))
    assert ".claude/settings.json" not in manifest["files"]


def test_standard_installer_rejects_sudo_before_writes(tmp_path):
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    profile = _make_oidc_central_profile()
    suffix = "macos-arm64" if os.uname().machine == "arm64" else "macos-intel"
    binary = package_dir / f"credential-process-{suffix}"
    binary.write_text("binary", encoding="utf-8")
    _run_config_phase(profile, package_dir, built_executables=[(suffix, binary)])
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "USER": "root", "SUDO_USER": "gip-test-user"}

    result = subprocess.run(  # nosec B603 -- fixed argv, test-controlled input
        ["bash", "install.sh"], cwd=package_dir, env=env, capture_output=True, text=True, check=False
    )

    assert result.returncode != 0
    assert "Do not run this installer with sudo or as root" in result.stdout
    assert not (home / "gip").exists()


def test_windows_manifest_is_recorded_before_success_prompt(tmp_path):
    installer = (
        PackageCommand()._create_windows_installer(tmp_path, _make_oidc_central_profile()).read_text(encoding="utf-8")
    )

    preflight = (tmp_path / "gip-install.ps1").read_text(encoding="utf-8")
    assert installer.index("-InstallAll") < installer.index("Installation complete!")
    assert "aws_sso_sessions" in preflight
    assert preflight.index("$manifestStaged") < preflight.index("Invoke-GipFileTransaction $entries")


def test_windows_preflight_runs_before_first_install_write(tmp_path):
    installer = (
        PackageCommand()._create_windows_installer(tmp_path, _make_oidc_central_profile()).read_text(encoding="utf-8")
    )
    preflight = (tmp_path / "gip-install.ps1").read_text(encoding="utf-8")

    assert installer.index('gip-install.ps1"') < installer.index("-InstallAll")
    assert "Refusing to overwrite $classification target" in preflight
    assert "Refusing to overwrite $classification AWS profile" in preflight
    assert "Do not run the full installer as Administrator" in preflight
    assert "-InstallManaged" in installer


def test_idc_installer_rejects_standard_mode_manifest_before_writes(tmp_path):
    standard_package = tmp_path / "standard"
    standard_package.mkdir()
    suffix = "macos-arm64" if os.uname().machine == "arm64" else "macos-intel"
    binary = standard_package / f"credential-process-{suffix}"
    binary.write_text("binary", encoding="utf-8")
    binary.chmod(0o755)
    _run_config_phase(
        _make_oidc_central_profile(),
        standard_package,
        built_executables=[(suffix, binary)],
    )

    idc_package = tmp_path / "idc"
    idc_package.mkdir()
    _run_config_phase(_make_idc_zero_binary_profile(), idc_package)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_aws = fake_bin / "aws"
    fake_aws.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_aws.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "USER": "gip-test-user",
        "SUDO_USER": "",
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }

    first = subprocess.run(  # nosec B603 -- fixed argv, test-controlled input
        ["bash", "install.sh"], cwd=standard_package, env=env, capture_output=True, text=True, check=False
    )
    assert first.returncode == 0, first.stderr or first.stdout
    manifest_path = home / "gip" / ".gip-ownership.json"
    manifest_before = manifest_path.read_bytes()
    second = subprocess.run(  # nosec B603 -- fixed argv, test-controlled input
        ["bash", "install.sh"], cwd=idc_package, env=env, capture_output=True, text=True, check=False
    )

    assert second.returncode != 0
    assert "another installer mode" in second.stdout
    assert manifest_path.read_bytes() == manifest_before
    assert (home / "gip" / "credential-process").read_text(encoding="utf-8") == "binary"


def test_idc_reinstall_accepts_crlf_aws_config_without_duplicate_sections(tmp_path):
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    _run_config_phase(_make_idc_zero_binary_profile(), package_dir)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_aws = fake_bin / "aws"
    fake_aws.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_aws.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "USER": "gip-test-user",
        "SUDO_USER": "",
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }

    first = subprocess.run(  # nosec B603 -- fixed argv, test-controlled input
        ["bash", "install.sh"], cwd=package_dir, env=env, capture_output=True, text=True, check=False
    )
    assert first.returncode == 0, first.stderr or first.stdout
    aws_config = home / ".aws" / "config"
    aws_config.write_bytes(aws_config.read_bytes().replace(b"\n", b"\r\n"))

    second = subprocess.run(  # nosec B603 -- fixed argv, test-controlled input
        ["bash", "install.sh"], cwd=package_dir, env=env, capture_output=True, text=True, check=False
    )

    assert second.returncode == 0, second.stderr or second.stdout
    content = aws_config.read_text(encoding="utf-8")
    assert content.count("[profile gip]") == 1
    assert content.count("[sso-session gip-session]") == 1


def test_generated_installers_use_no_clobber_transaction_markers(tmp_path):
    idc_dir = tmp_path / "idc"
    idc_dir.mkdir()
    _run_config_phase(_make_idc_zero_binary_profile(), idc_dir)
    idc = (idc_dir / "install.sh").read_text(encoding="utf-8")
    assert "gip_commit_snapshot" in idc
    assert "gip_restore_quarantine" in idc
    assert "mv -f" not in idc

    windows = PackageCommand()._create_windows_installer(tmp_path, _make_oidc_central_profile())
    batch = windows.read_text(encoding="utf-8")
    preflight = (tmp_path / "gip-install.ps1").read_text(encoding="utf-8")
    assert "-EncodedCommand" in batch
    assert "-UserHome '$home'" in batch
    assert "-InstallAll" in batch
    assert batch.count("-InstallAll") == 1
    assert "-InstallFile" not in batch
    assert "-ConfigureAws" not in batch
    assert "-RecordManifest" not in batch
    assert "Ownership manifest appeared during installation" in preflight
    assert '[string]$UserHome = ""' in preflight
    assert ".managed-settings.gip-sha256" in preflight
    assert "Invoke-GipFileTransaction" in preflight
    assert "[IO.File]::Move" in preflight
    assert "Commit succeeded, but previous files remain at:" in preflight
    assert "elseif (Test-Path -LiteralPath $entry.Target)" in preflight
    assert "$collectorActive = $installCollector -or $retainedCollector" in preflight
    assert "if ($collectorActive) { $profileNames +=" in preflight
    assert "Refusing duplicate AWS profile sections" in preflight
    assert batch.index("-InstallAll") < batch.index("-InstallManaged")
    assert "[IO.File]::Replace" not in preflight
    assert "copy /Y" not in batch
    assert "aws configure set" not in batch
    assert "Add-Content" not in batch


# ---------------------------------------------------------------------------
# Tests: Structural Wiring (source-level guards)
# ---------------------------------------------------------------------------


class TestPackageWiringCompleteness:
    """Source-level assertions that handle() calls all required build methods.

    These tests use inspect.getsource() to verify call sites exist, catching
    deletions in code review (like the PR #338 otelcol regression).
    """

    def test_handle_calls_build_go_binaries(self):
        """handle() must call _build_go_binaries for Go cross-compilation."""
        import inspect

        src = inspect.getsource(PackageCommand.handle)
        assert "_build_go_binaries" in src, (
            "_build_go_binaries call missing from handle() — Go cross-compilation is broken."
        )

    def test_handle_calls_build_otelcol(self):
        """handle() must call _build_otelcol for sidecar profiles."""
        import inspect

        src = inspect.getsource(PackageCommand.handle)
        assert "_build_otelcol" in src, (
            "_build_otelcol call missing from handle() — sidecar packages will not include the collector binary."
        )

    def test_handle_calls_generate_collector_config(self):
        """handle() must call _generate_collector_config for sidecar profiles."""
        import inspect

        src = inspect.getsource(PackageCommand.handle)
        assert "_generate_collector_config" in src, (
            "_generate_collector_config call missing from handle() — "
            "sidecar packages will ship without collector-config.yaml."
        )

    def test_handle_has_idc_zero_binary_guard(self):
        """handle() must skip binary builds for IDC zero-binary mode."""
        import inspect

        src = inspect.getsource(PackageCommand.handle)
        assert "is_idc_zero_binary" in src, (
            "IDC zero-binary guard missing from handle() — would attempt to build binaries on machines without Go."
        )

    def test_sidecar_guard_on_otelcol_build(self):
        """_build_otelcol call must be guarded by sidecar mode check."""
        import inspect

        src = inspect.getsource(PackageCommand.handle)
        # Verify both the call and the sidecar guard exist
        assert "_build_otelcol" in src
        assert "sidecar" in src, (
            "No sidecar guard around _build_otelcol — collector would be built unnecessarily for central mode."
        )
