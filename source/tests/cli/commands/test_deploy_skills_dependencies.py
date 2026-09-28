"""Tests for packaging skills registry Lambda dependencies."""

import sys
from unittest.mock import Mock, patch

import pytest

from governed_inference_platform.cli.commands.deploy import _stage_skills_registry_template


def test_stage_skills_registry_template_bundles_pinned_sdk(tmp_path):
    project_root = tmp_path / "project"
    source_dir = project_root / "deployment" / "infrastructure" / "lambda-functions" / "skills_registry"
    source_dir.mkdir(parents=True)
    (project_root / "deployment" / "infrastructure" / "skills-registry.yaml").write_text(
        "Resources: {}\n", encoding="utf-8"
    )
    (source_dir / "index.py").write_text("def handler(event, context): return {}\n", encoding="utf-8")
    (source_dir / "requirements.txt").write_text("boto3==1.43.70\nbotocore==1.43.70\n", encoding="utf-8")

    with (
        patch("governed_inference_platform.cli.commands.deploy.importlib.util.find_spec", return_value=object()),
        patch(
            "governed_inference_platform.cli.commands.deploy.run_checked",
            return_value=Mock(returncode=0, stderr=""),
        ) as run,
    ):
        template = _stage_skills_registry_template(project_root, tmp_path / "build")

    assert template.read_text(encoding="utf-8") == "Resources: {}\n"
    staged_lambda = template.parent / "lambda-functions" / "skills_registry"
    assert (staged_lambda / "index.py").exists()
    command = run.call_args.args[0]
    assert command[1:4] == ["-m", "pip", "install"]
    assert command[command.index("--requirement") + 1] == str(source_dir / "requirements.txt")
    assert command[command.index("--target") + 1] == str(staged_lambda)


def test_stage_skills_registry_template_fails_when_dependency_bundle_fails(tmp_path):
    project_root = tmp_path / "project"
    source_dir = project_root / "deployment" / "infrastructure" / "lambda-functions" / "skills_registry"
    source_dir.mkdir(parents=True)
    (project_root / "deployment" / "infrastructure" / "skills-registry.yaml").write_text(
        "Resources: {}\n", encoding="utf-8"
    )
    (source_dir / "requirements.txt").write_text("boto3==1.43.70\nbotocore==1.43.70\n", encoding="utf-8")

    with (
        patch(
            "governed_inference_platform.cli.commands.deploy.run_checked",
            return_value=Mock(returncode=1, stderr="package unavailable"),
        ),
        pytest.raises(RuntimeError, match="package unavailable"),
    ):
        _stage_skills_registry_template(project_root, tmp_path / "build")


def test_stage_skills_registry_template_uses_uv_when_venv_has_no_pip(tmp_path):
    project_root = tmp_path / "project"
    source_dir = project_root / "deployment" / "infrastructure" / "lambda-functions" / "skills_registry"
    source_dir.mkdir(parents=True)
    (project_root / "deployment" / "infrastructure" / "skills-registry.yaml").write_text(
        "Resources: {}\n", encoding="utf-8"
    )
    (source_dir / "requirements.txt").write_text("boto3==1.43.70\n", encoding="utf-8")

    with (
        patch("governed_inference_platform.cli.commands.deploy.importlib.util.find_spec", return_value=None),
        patch("governed_inference_platform.cli.commands.deploy.shutil.which", return_value="/usr/local/bin/uv"),
        patch(
            "governed_inference_platform.cli.commands.deploy.run_checked",
            return_value=Mock(returncode=0, stderr=""),
        ) as run,
    ):
        _stage_skills_registry_template(project_root, tmp_path / "build")

    assert run.call_args.args[0][:6] == [
        "/usr/local/bin/uv",
        "pip",
        "install",
        "--python",
        sys.executable,
        "--disable-pip-version-check",
    ]
