import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.support.proc import run_cmd

REPO_ROOT = Path(__file__).resolve().parents[2]
SECURITY_HOOK = REPO_ROOT / "assets" / "claude-code-plugins" / "plugins" / "security" / "hooks" / "security_check.py"


def run_security_hook(input_text: str, project_root: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(project_root)
    return run_cmd(  # nosec B603
        [sys.executable, str(SECURITY_HOOK)],
        input=input_text,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def load_security_hook():
    spec = importlib.util.spec_from_file_location("security_check", SECURITY_HOOK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_security_hook_blocks_invalid_json(tmp_path):
    result = run_security_hook("{", tmp_path)

    assert result.returncode == 2
    assert "Invalid JSON input" in result.stderr


def test_security_hook_keeps_audit_logging_best_effort(tmp_path):
    work_file = tmp_path / "safe.py"
    work_file.write_text("print('ok')\n")
    (tmp_path / ".claude").write_text("not a directory")

    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(work_file),
            "content": "print('ok')\n",
        },
    }
    result = run_security_hook(json.dumps(payload), tmp_path)

    assert result.returncode == 0
    assert "Security hook audit warning" in result.stderr


def test_security_hook_still_blocks_secrets(tmp_path):
    work_file = tmp_path / "secret.py"
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(work_file),
            "content": 'api_key="abcdefghijklmnopqrstuvwxyz"',
        },
    }
    result = run_security_hook(json.dumps(payload), tmp_path)

    assert result.returncode == 2
    assert "Potential secrets detected" in result.stderr


def test_security_hook_blocks_windows_style_git_hook_paths(tmp_path):
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(tmp_path / ".git\\hooks\\pre-commit"),
            "content": "print('ok')\n",
        },
    }
    result = run_security_hook(json.dumps(payload), tmp_path)

    assert result.returncode == 2
    assert "Suspicious file path" in result.stderr


def test_security_hook_blocks_nested_hidden_security_paths(tmp_path):
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(tmp_path / "nested" / ".git\\hooks\\pre-commit"),
            "content": "print('ok')\n",
        },
    }
    result = run_security_hook(json.dumps(payload), tmp_path)

    assert result.returncode == 2
    assert "Suspicious file path" in result.stderr


def test_security_hook_blocks_multiedit_secrets(tmp_path):
    work_file = tmp_path / "secret.py"
    payload = {
        "tool_name": "MultiEdit",
        "tool_input": {
            "file_path": str(work_file),
            "edits": [
                {
                    "old_string": "",
                    "new_string": 'api_key="abcdefghijklmnopqrstuvwxyz"',
                }
            ],
        },
    }
    result = run_security_hook(json.dumps(payload), tmp_path)

    assert result.returncode == 2
    assert "Potential secrets detected" in result.stderr


def test_security_hook_blocks_malformed_multiedit_payload(tmp_path):
    work_file = tmp_path / "safe.py"
    payload = {
        "tool_name": "MultiEdit",
        "tool_input": {
            "file_path": str(work_file),
            "edits": [{"old_string": "print('old')"}],
        },
    }
    result = run_security_hook(json.dumps(payload), tmp_path)

    assert result.returncode == 2
    assert "MultiEdit edit 0 must include old_string and new_string" in result.stderr


def test_security_hook_permission_errors_fail_closed(monkeypatch):
    security_hook = load_security_hook()

    def fail_stat(_file_path):
        raise PermissionError("denied")

    monkeypatch.setattr(security_hook.os, "stat", fail_stat)

    with pytest.raises(RuntimeError, match="Unable to verify permissions"):
        security_hook.check_file_permissions("safe.py")
