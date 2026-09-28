"""Tests for credential binary and packaged profile resolution."""

import base64
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from governed_inference_platform.cli.commands.test import TestCommand as GIPTestCommand


@pytest.mark.parametrize("method", ["_test_quota_api", "_get_user_email_from_jwt"])
def test_credential_binary_resolves_before_subprocess_cwd_change(tmp_path, monkeypatch, method):
    binary = tmp_path / "credential-process"
    binary.touch()
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    monkeypatch.chdir(tmp_path)
    payload = base64.urlsafe_b64encode(json.dumps({"email": "alice@example.com"}).encode()).decode().rstrip("=")
    token_result = MagicMock(returncode=0, stdout=f"header.{payload}.signature", stderr="")

    with patch("subprocess.run", return_value=token_result) as run:
        if method == "_test_quota_api":
            response = MagicMock()
            response.read.return_value = b'{"allowed": true, "reason": "ok"}'
            response.__enter__.return_value = response
            with patch("urllib.request.urlopen", return_value=response):
                GIPTestCommand()._test_quota_api(Path("credential-process"), "https://quota", package_dir, "test")
        else:
            GIPTestCommand()._get_user_email_from_jwt(Path("credential-process"), package_dir, "test")

    assert run.call_args.args[0][0] == str(binary.resolve())
    assert run.call_args.kwargs["cwd"] == package_dir


def test_package_profile_name_reads_nested_go_config_schema(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"profiles": {"gipval": {"aws_region": "us-east-1"}}}))

    assert GIPTestCommand()._get_package_profile_name(tmp_path) == "gipval"


def test_package_profile_name_accepts_legacy_flat_schema(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"legacy": {"aws_region": "us-east-1"}}))

    assert GIPTestCommand()._get_package_profile_name(tmp_path) == "legacy"
