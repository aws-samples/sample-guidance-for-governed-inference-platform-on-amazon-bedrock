"""Public-release container references must use ECR Public where equivalents exist."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_generated_linux_build_dockerfiles_use_ecr_public_ubuntu():
    package_source = (REPO_ROOT / "source/governed_inference_platform/cli/commands/package.py").read_text(
        encoding="utf-8"
    )
    ecr_from = (
        "FROM --platform={docker_platform} public.ecr.aws/docker/library/ubuntu@sha256:"
        "2edbbc5dc405e9612ba3584ce95480277e3eb374407b5505fe26f17df77c7dbc"
    )

    assert package_source.count(ecr_from) == 2
    assert "FROM --platform={docker_platform} ubuntu:22.04" not in package_source
    assert "public.ecr.aws/docker/library/ubuntu:22.04" not in package_source
