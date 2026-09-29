# ABOUTME: Executes the generated Windows installer transaction primitive on native Windows.
# ABOUTME: Covers owned replacement and rollback after a no-overwrite commit conflict.

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from governed_inference_platform.cli.commands.package import PackageCommand
from governed_inference_platform.config import Profile

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="requires Windows PowerShell and NTFS semantics")


def _profile() -> Profile:
    return Profile(
        name="windows-transaction-test",
        provider_domain="auth.example.com",
        client_id="client-id",
        credential_storage="keyring",
        aws_region="us-east-1",
        identity_pool_name="pool",
    )


def _install_bat_environment() -> dict[str, str]:
    """The environment install.bat gives Windows PowerShell: the caller's, minus PSModulePath.

    These tests start powershell.exe directly, so they must clear the variable the way
    install.bat does; otherwise a PowerShell 7 parent leaks its module path into
    Windows PowerShell and Get-FileHash is not found.
    """
    return {key: value for key, value in os.environ.items() if key.upper() != "PSMODULEPATH"}


def _run_harness(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    PackageCommand()._create_windows_installer(tmp_path, _profile())
    harness = tmp_path / "transaction-test.ps1"
    harness.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        ". (Join-Path $PSScriptRoot 'gip-install.ps1') -TransactionLibraryOnly\n" + body,
        encoding="utf-8",
    )
    return subprocess.run(  # nosec B603 -- fixed argv, test-controlled input
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
        cwd=tmp_path,
        env=_install_bat_environment(),
        capture_output=True,
        text=True,
        check=False,
    )


def _is_administrator() -> bool:
    result = subprocess.run(  # nosec B603 -- fixed argv, test-controlled input
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "(New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip().lower() == "true"


def _run_batch(tmp_path: Path, home: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603 -- fixed argv, test-controlled input
        ["cmd.exe", "/d", "/c", "install.bat"],
        cwd=tmp_path,
        env={**os.environ, "USERPROFILE": str(home)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )


def test_generated_transaction_replaces_only_expected_owned_file(tmp_path):
    result = _run_harness(
        tmp_path,
        r"""
$target = Join-Path $PSScriptRoot 'owned.txt'
$staged = Join-Path $PSScriptRoot 'staged.txt'
[IO.File]::WriteAllText($target, 'old')
[IO.File]::WriteAllText($staged, 'new')
$expected = Get-GipDigest $target
Invoke-GipFileTransaction @([PSCustomObject]@{ Target = $target; Staged = $staged; ExpectedDigest = $expected })
if ([IO.File]::ReadAllText($target) -ne 'new') { throw 'replacement did not commit' }
""",
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_generated_transaction_rolls_back_first_commit_when_second_conflicts(tmp_path):
    result = _run_harness(
        tmp_path,
        r"""
$target = Join-Path $PSScriptRoot 'appeared.txt'
$first = Join-Path $PSScriptRoot 'first.txt'
$second = Join-Path $PSScriptRoot 'second.txt'
[IO.File]::WriteAllText($first, 'first')
[IO.File]::WriteAllText($second, 'second')
$failed = $false
try {
    Invoke-GipFileTransaction @(
        [PSCustomObject]@{ Target = $target; Staged = $first; ExpectedDigest = $null },
        [PSCustomObject]@{ Target = $target; Staged = $second; ExpectedDigest = $null }
    )
} catch { $failed = $true }
if (-not $failed) { throw 'duplicate destination unexpectedly committed' }
if (Test-Path -LiteralPath $target) { throw 'transaction-owned first commit was not rolled back' }
""",
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_install_all_commits_files_aws_config_and_manifest_together(tmp_path):
    home = tmp_path / "user home"
    home.mkdir()
    (tmp_path / "credential-process-windows.exe").write_bytes(b"credential")
    (tmp_path / "config.json").write_text(
        json.dumps({"profiles": {"windows-transaction-test": {"aws_region": "us-east-1"}}}),
        encoding="utf-8",
    )
    PackageCommand()._create_windows_installer(tmp_path, _profile())

    result = subprocess.run(  # nosec B603 -- fixed argv, test-controlled input
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(tmp_path / "gip-install.ps1"),
            "-InstallAll",
            "-UserHome",
            str(home),
        ],
        cwd=tmp_path,
        env=_install_bat_environment(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    install = home / "gip"
    assert (install / "credential-process.exe").read_bytes() == b"credential"
    assert (install / "config.json").is_file()
    manifest = json.loads((install / ".gip-ownership.json").read_text(encoding="utf-8"))
    assert manifest["files"]["gip/credential-process.exe"]
    aws_config = (home / ".aws" / "config").read_text(encoding="utf-8")
    assert "[profile windows-transaction-test]" in aws_config


def test_install_all_rejects_duplicate_aws_sections_without_committing(tmp_path):
    home = tmp_path / "home"
    aws_config = home / ".aws" / "config"
    aws_config.parent.mkdir(parents=True)
    original = (
        "[profile windows-transaction-test]\nregion = us-east-1\n"
        "[profile windows-transaction-test]\nregion = us-west-2\n"
    )
    aws_config.write_text(original, encoding="utf-8")
    (tmp_path / "credential-process-windows.exe").write_bytes(b"credential")
    (tmp_path / "config.json").write_text(
        json.dumps({"profiles": {"windows-transaction-test": {"aws_region": "us-east-1"}}}), encoding="utf-8"
    )
    PackageCommand()._create_windows_installer(tmp_path, _profile())

    result = subprocess.run(  # nosec B603 -- fixed argv, test-controlled input
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(tmp_path / "gip-install.ps1"),
            "-InstallAll",
            "-UserHome",
            str(home),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "duplicate AWS profile sections" in result.stderr
    assert aws_config.read_text(encoding="utf-8") == original
    assert not (home / "gip").exists()


def test_generated_batch_executes_end_to_end_with_spaced_userprofile(tmp_path):
    home = tmp_path / "user home"
    home.mkdir()
    (tmp_path / "credential-process-windows.exe").write_bytes(b"credential")
    (tmp_path / "otel-helper.cmd").write_text("@echo off\r\n", encoding="utf-8")
    (tmp_path / "otel-helper.ps1").write_text("{}\n", encoding="utf-8")
    (tmp_path / "config.json").write_text(
        json.dumps({"profiles": {"windows-transaction-test": {"aws_region": "us-east-1"}}}), encoding="utf-8"
    )
    (tmp_path / "cowork-3p.reg").write_text("home=__GIP_HOME__\n", encoding="utf-8")
    settings = tmp_path / "gip-settings"
    settings.mkdir()
    (settings / "mcp.json").write_text('{"headersHelper":"__WEBSEARCH_HEADERS_HELPER__"}\n', encoding="utf-8")
    opencode = tmp_path / "harnesses" / "opencode"
    opencode.mkdir(parents=True)
    (opencode / "opencode.json").write_text('{"command":"__CREDENTIAL_PROCESS_PATH__"}\n', encoding="utf-8")
    package_files = [tmp_path / "cowork-3p.reg", settings / "mcp.json", opencode / "opencode.json"]
    fingerprints = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in package_files}
    PackageCommand()._create_windows_installer(tmp_path, _profile())

    result = _run_batch(tmp_path, home)

    if _is_administrator():
        assert result.returncode != 0
        assert "Do not run the full installer as Administrator" in result.stderr
        pytest.skip("end-to-end install.bat success requires a standard-user Windows runner")
    assert result.returncode == 0, result.stderr or result.stdout
    assert (home / "gip" / ".gip-ownership.json").is_file()
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in package_files} == fingerprints
    assert "__GIP_HOME__" not in (home / "gip" / "cowork-3p.reg").read_text(encoding="utf-8")
    assert "__WEBSEARCH_HEADERS_HELPER__" not in (home / "gip" / "gip-settings" / "mcp.json").read_text(
        encoding="utf-8"
    )
    assert "__CREDENTIAL_PROCESS_PATH__" not in (home / "gip" / "harnesses" / "opencode" / "opencode.json").read_text(
        encoding="utf-8"
    )
