# ABOUTME: Contract tests for the default-on RestrictToAnthropicModels parameter across auth templates
# ABOUTME: Ensures the server-side Anthropic model allow-list is wired consistently

"""Tests for the default-on server-side Anthropic model allow-list.

Every auth template that grants bedrock:InvokeModel on foundation-model/*
must expose a RestrictToAnthropicModels parameter (Default 'true'), an
AnthropicModelsOnly condition, and an
Anthropic-scoped resource list selected via !If.
"""

from pathlib import Path

import pytest
import yaml

INFRA_DIR = Path(__file__).parent.parent.parent / "deployment" / "infrastructure"
DEPLOY_PY = Path(__file__).parent.parent / "governed_inference_platform" / "cli" / "commands" / "deploy.py"

MODIFIED_TEMPLATES = [
    "bedrock-auth-okta.yaml",
    "bedrock-auth-azure.yaml",
    "bedrock-auth-auth0.yaml",
    "bedrock-auth-google.yaml",
    "bedrock-auth-generic.yaml",
    "bedrock-auth-cognito-pool.yaml",
    "bedrock-auth-idc.yaml",
    "cognito-identity-pool.yaml",
]


class CFLoader(yaml.SafeLoader):
    """YAML loader that handles CloudFormation intrinsic functions."""

    pass


# Register CF intrinsic constructors
for tag in [
    "!Ref",
    "!Sub",
    "!GetAtt",
    "!If",
    "!Equals",
    "!Not",
    "!Select",
    "!Join",
    "!Split",
    "!FindInMap",
    "!Condition",
    "!Or",
    "!And",
]:
    CFLoader.add_constructor(
        tag,
        lambda loader, node: (
            loader.construct_scalar(node)
            if isinstance(node, yaml.ScalarNode)
            else loader.construct_sequence(node)
            if isinstance(node, yaml.SequenceNode)
            else loader.construct_mapping(node)
        ),
    )


def _load_template(name: str) -> dict:
    path = INFRA_DIR / name
    if not path.exists():
        pytest.skip(f"Template {name} not found")
    with open(path, encoding="utf-8") as f:
        return yaml.load(f, Loader=CFLoader)  # nosec B506


def _collect_strings(node) -> list:
    """Recursively collect all string scalars from a parsed template."""
    strings = []
    if isinstance(node, str):
        strings.append(node)
    elif isinstance(node, dict):
        for key, value in node.items():
            strings.extend(_collect_strings(key))
            strings.extend(_collect_strings(value))
    elif isinstance(node, list):
        for item in node:
            strings.extend(_collect_strings(item))
    return strings


class TestRestrictToAnthropicModels:
    """The model allow-list must be default-on and consistently wired."""

    @pytest.mark.parametrize("template_name", MODIFIED_TEMPLATES)
    def test_parameter_exists_with_safe_default(self, template_name):
        """RestrictToAnthropicModels exists and defaults to server-side model scoping."""
        doc = _load_template(template_name)
        params = doc.get("Parameters", {})
        assert "RestrictToAnthropicModels" in params, f"{template_name} missing RestrictToAnthropicModels parameter"
        param = params["RestrictToAnthropicModels"]
        assert param.get("Type") == "String"
        assert param.get("Default") == "true", f"{template_name}: Default must be 'true' for fail-closed scoping"
        assert sorted(param.get("AllowedValues", [])) == ["false", "true"]

    @pytest.mark.parametrize("template_name", MODIFIED_TEMPLATES)
    def test_condition_exists(self, template_name):
        """The positively-named AnthropicModelsOnly condition exists."""
        doc = _load_template(template_name)
        conditions = doc.get("Conditions", {})
        assert "AnthropicModelsOnly" in conditions, f"{template_name} missing AnthropicModelsOnly condition"

    @pytest.mark.parametrize("template_name", MODIFIED_TEMPLATES)
    def test_anthropic_scoped_resources_present(self, template_name):
        """The Anthropic-scoped branch contains anthropic.* resource patterns."""
        doc = _load_template(template_name)
        strings = _collect_strings(doc.get("Resources", {}))
        assert any("foundation-model/anthropic.*" in s for s in strings), (
            f"{template_name} missing foundation-model/anthropic.* pattern"
        )
        assert any(":inference-profile/*.anthropic.*" in s for s in strings), (
            f"{template_name} missing inference-profile/*.anthropic.* pattern"
        )

    def test_deploy_passes_default_on_parameter(self):
        """CLI deployments must not rely on stale CloudFormation defaults."""
        src = DEPLOY_PY.read_text(encoding="utf-8")
        assert "RestrictToAnthropicModels=" in src
        assert "restrict_to_anthropic_models" in src
