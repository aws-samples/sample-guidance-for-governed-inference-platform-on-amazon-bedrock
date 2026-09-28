import importlib.util
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHON_LINT_HOOK = REPO_ROOT / "assets" / "claude-code-plugins" / "plugins" / "testing" / "hooks" / "python_lint.py"


def load_python_lint_hook():
    spec = importlib.util.spec_from_file_location("python_lint", PYTHON_LINT_HOOK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_black_internal_errors_warn_without_blocking(monkeypatch):
    python_lint = load_python_lint_hook()

    def fail_black(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            ["black"],
            returncode=123,
            stdout="",
            stderr="internal black error",
        )

    monkeypatch.setattr(python_lint.subprocess, "run", fail_black)

    result = python_lint.check_formatting("safe.py")

    assert result == {
        "tool": "black (formatting)",
        "passed": True,
        "error": "internal black error",
    }


def test_ruff_stderr_only_failures_warn_without_blocking(monkeypatch):
    python_lint = load_python_lint_hook()

    def fail_ruff(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            ["ruff"],
            returncode=2,
            stdout="",
            stderr="invalid ruff config",
        )

    monkeypatch.setattr(python_lint.subprocess, "run", fail_ruff)

    result = python_lint.run_ruff("safe.py")

    assert result == {
        "tool": "ruff",
        "passed": True,
        "error": "invalid ruff config",
    }


def test_flake8_fallback_output_counts_as_diagnostics():
    python_lint = load_python_lint_hook()
    error_messages = []

    has_errors = python_lint.add_lint_diagnostic_errors(
        {"tool": "flake8", "passed": False, "output": "safe.py:1:1: F401"},
        error_messages,
    )

    assert has_errors is True
    assert error_messages == ["flake8: Found issues"]


def test_mypy_stderr_only_failures_warn_without_blocking():
    python_lint = load_python_lint_hook()
    error_messages = []

    has_errors = python_lint.add_mypy_diagnostic_errors(
        {"tool": "mypy", "passed": False, "output": "", "error": "bad mypy config"},
        error_messages,
    )

    assert has_errors is False
    assert error_messages == []


def test_mypy_stdout_diagnostics_count_as_type_errors():
    python_lint = load_python_lint_hook()
    error_messages = []

    has_errors = python_lint.add_mypy_diagnostic_errors(
        {"tool": "mypy", "passed": False, "output": "safe.py: error", "error": ""},
        error_messages,
    )

    assert has_errors is True
    assert error_messages == ["Mypy: Type checking issues found"]
