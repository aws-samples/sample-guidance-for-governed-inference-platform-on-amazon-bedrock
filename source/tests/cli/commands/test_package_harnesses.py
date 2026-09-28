# ABOUTME: Tests that gip package --harnesses writes harnesses/<name>/ configs into the output.
# ABOUTME: Default selection (claude-code only) must produce exactly today's output — no harnesses dir.

"""Package-level tests for the --harnesses option (multi-harness config generation)."""

import json
from unittest.mock import MagicMock

from governed_inference_platform.cli.commands.package import PackageCommand
from governed_inference_platform.cli.utils.harness_configs import HARNESS_GENERATORS, resolve_harness_selection
from governed_inference_platform.config import Profile
from governed_inference_platform.models import resolve_model_for_tier


def _make_profile(**overrides):
    defaults = {
        "name": "gip",
        "provider_domain": "auth.example.com",
        "client_id": "client-abc",
        "credential_storage": "session",
        "aws_region": "us-east-1",
        "identity_pool_name": "test-pool",
        "auth_type": "oidc",
        "monitoring_enabled": False,
        "cross_region_profile": "us",
    }
    defaults.update(overrides)
    return Profile(**defaults)


def _run_harness_phase(profile, output_dir, selection):
    """Run the harness-generation phase exactly as PackageCommand.handle does."""
    extra = resolve_harness_selection(selection)
    if extra:
        PackageCommand()._create_harness_configs(output_dir, profile, profile.name, extra, MagicMock())
    return extra


class TestDefaultBehaviorUnchanged:
    def test_default_selection_writes_no_harnesses_dir(self, tmp_path):
        profile = _make_profile()
        extra = _run_harness_phase(profile, tmp_path, ",".join(profile.harnesses))
        assert extra == []
        assert not (tmp_path / "harnesses").exists()

    def test_profile_default_harnesses_is_claude_code_only(self):
        assert _make_profile().harnesses == ["claude-code"]

    def test_old_config_without_harnesses_field_loads_with_default(self):
        profile = Profile.from_dict(
            {
                "name": "legacy",
                "provider_domain": "auth.example.com",
                "client_id": "client-abc",
                "aws_region": "us-east-1",
                "identity_pool_name": "pool",
            }
        )
        assert profile.harnesses == ["claude-code"]


class TestHarnessesAll:
    def test_all_writes_every_harness_dir_and_readme(self, tmp_path):
        profile = _make_profile()
        extra = _run_harness_phase(profile, tmp_path, "all")
        assert extra == list(HARNESS_GENERATORS)
        assert (tmp_path / "harnesses" / "README.md").exists()
        assert (tmp_path / "harnesses" / "opencode" / "opencode.json").exists()
        assert (tmp_path / "harnesses" / "codex" / "config.toml").exists()
        assert (tmp_path / "harnesses" / "pi" / "pi-bedrock.env").exists()
        assert (tmp_path / "harnesses" / "aider" / ".aider.conf.yml").exists()

    def test_opencode_config_uses_same_cris_ids_as_claude_settings(self, tmp_path):
        """Model IDs must be the SAME resolved CRIS profile IDs Claude Code gets."""
        profile = _make_profile(cross_region_profile="eu")
        _run_harness_phase(profile, tmp_path, "opencode")
        parsed = json.loads((tmp_path / "harnesses" / "opencode" / "opencode.json").read_text())
        models = parsed["provider"]["amazon-bedrock"]["models"]
        for tier in ("haiku", "sonnet", "opus"):
            expected = resolve_model_for_tier(tier, "eu")
            if expected:
                assert expected in models

    def test_readme_references_aws_profile_and_region(self, tmp_path):
        profile = _make_profile()
        _run_harness_phase(profile, tmp_path, "all")
        readme = (tmp_path / "harnesses" / "README.md").read_text()
        assert "gip" in readme
        assert "us-east-1" in readme

    def test_single_harness_writes_only_that_dir(self, tmp_path):
        profile = _make_profile()
        _run_harness_phase(profile, tmp_path, "aider")
        assert (tmp_path / "harnesses" / "aider" / ".aider.conf.yml").exists()
        assert not (tmp_path / "harnesses" / "opencode").exists()
        assert not (tmp_path / "harnesses" / "codex").exists()
        assert not (tmp_path / "harnesses" / "pi").exists()

    def test_aider_and_pi_default_to_profile_selected_model(self, tmp_path):
        selected = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
        profile = _make_profile(selected_model=selected)
        _run_harness_phase(profile, tmp_path, "pi,aider")
        aider = (tmp_path / "harnesses" / "aider" / ".aider.conf.yml").read_text()
        pi = (tmp_path / "harnesses" / "pi" / "pi-bedrock.env").read_text()
        assert f"model: bedrock/{selected}" in aider
        assert f"--provider amazon-bedrock --model {selected}" in pi


class TestCommandWiring:
    def test_harnesses_option_registered_with_none_default(self):
        opts = {o.name: o for o in PackageCommand.options}
        assert "harnesses" in opts
        assert opts["harnesses"].default is None
