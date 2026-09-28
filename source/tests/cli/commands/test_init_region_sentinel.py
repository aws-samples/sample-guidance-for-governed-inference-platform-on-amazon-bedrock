# ABOUTME: Regression tests for the "all-commercial" sentinel leaking into allowed_bedrock_regions
# ABOUTME: Covers interactive/answers-file parity and rendered AllowedBedrockRegions param correctness

"""Regression tests for the global-CRIS region sentinel (review finding C-1).

Global-CRIS catalog entries carry ``destination_regions: ["all-commercial"]``.
Before the fix, interactive init assigned that sentinel verbatim to
``allowed_bedrock_regions``, which then:
- rendered into the auth stack's ``AllowedBedrockRegions`` CFN param, producing
  an ``aws:RequestedRegion`` condition that matches no region (all invokes denied),
- was used as a boto3 region name by the per-region metering loop (crash),
while the answers-file path *rejected* the same value in validation.

The fix expands the sentinel into the real commercial region list at assignment
time in both init paths (``expand_region_sentinels``).
"""

import json
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.support.cli import CliTester

# ruff: noqa: E402
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from governed_inference_platform.cli.commands.init import InitCommand
from governed_inference_platform.cli.utils.validators import validate_aws_region
from governed_inference_platform.config import Config
from governed_inference_platform.models import (
    CLAUDE_MODELS,
    expand_region_sentinels,
    get_destination_regions_for_model_profile,
)

INIT_SOURCE = (
    Path(__file__).resolve().parents[3] / "governed_inference_platform" / "cli" / "commands" / "init.py"
).read_text(encoding="utf-8")

GLOBAL_SENTINEL_COMBOS = [
    (model_key, profile_key)
    for model_key, model_config in CLAUDE_MODELS.items()
    for profile_key, profile_config in model_config["profiles"].items()
    if any(r.startswith("all-") for r in profile_config["destination_regions"])
]

GLOBAL_YAML = """
okta:
  domain: company.okta.com
  client_id: 0oa0000000000000000
aws:
  selected_model: global.anthropic.claude-sonnet-4-6
  cross_region_profile: global
"""

EXPLICIT_SENTINEL_YAML = """
okta:
  domain: company.okta.com
  client_id: 0oa0000000000000000
aws:
  selected_model: global.anthropic.claude-sonnet-4-6
  cross_region_profile: global
  allowed_bedrock_regions:
    - all-commercial
"""


@pytest.fixture
def config_paths(tmp_path):
    """Patch Config storage paths into tmp_path and stub out AWS stack checks."""
    config_dir = tmp_path / ".gip"
    config_dir.mkdir()
    profiles_dir = config_dir / "profiles"
    profiles_dir.mkdir()
    config_file = config_dir / "config.json"
    config_file.write_text(json.dumps({"schema_version": "2.0", "active_profile": None}))

    with (
        patch.object(Config, "CONFIG_DIR", config_dir),
        patch.object(Config, "CONFIG_FILE", config_file),
        patch.object(Config, "PROFILES_DIR", profiles_dir),
        patch.object(InitCommand, "_stack_exists", side_effect=Exception("no creds")),
    ):
        yield profiles_dir


@pytest.fixture
def tester():
    return CliTester(InitCommand())


def _load_profile(profiles_dir: Path, name: str) -> dict:
    return json.loads((profiles_dir / f"{name}.json").read_text())


class TestExpandRegionSentinels:
    """The expansion helper resolves sentinels to real commercial regions."""

    def test_catalog_has_global_sentinel_entries(self):
        # Guard: the scenario under test exists in the catalog.
        assert GLOBAL_SENTINEL_COMBOS, "expected global-CRIS entries with the all-commercial sentinel"

    @pytest.mark.parametrize("model_key,profile_key", GLOBAL_SENTINEL_COMBOS)
    def test_sentinel_expands_to_valid_commercial_regions(self, model_key, profile_key):
        expanded = expand_region_sentinels(get_destination_regions_for_model_profile(model_key, profile_key))
        assert expanded, f"{model_key}/{profile_key} expanded to an empty region list"
        for region in expanded:
            assert not region.startswith("all-"), f"sentinel leaked: {region}"
            assert "gov" not in region, f"GovCloud region in commercial expansion: {region}"
            assert validate_aws_region(region), f"not a valid AWS region: {region}"

    def test_non_sentinel_lists_pass_through_unchanged(self):
        regions = ["us-east-1", "eu-west-1"]
        assert expand_region_sentinels(regions) == regions

    def test_mixed_list_keeps_explicit_regions(self):
        expanded = expand_region_sentinels(["us-east-1", "all-commercial"])
        assert "us-east-1" in expanded
        assert "all-commercial" not in expanded


class TestInteractiveAssignment:
    """The interactive wizard expands the sentinel at assignment time."""

    def test_init_wizard_expands_sentinel_at_assignment(self):
        # The wizard's assignment is inline in a questionary-driven flow; pin the
        # source so the expansion cannot silently disappear from the interactive path.
        assert re.search(
            r'config\["aws"\]\["allowed_bedrock_regions"\]\s*=\s*expand_region_sentinels\(',
            INIT_SOURCE,
        ), "init.py must expand region sentinels when assigning allowed_bedrock_regions"


class TestAnswersFileParity:
    """Answers-file init derives the same expanded regions as interactive init."""

    def test_global_model_derives_expanded_regions(self, config_paths, tester, tmp_path):
        answers = tmp_path / "answers.yaml"
        answers.write_text(GLOBAL_YAML)

        rc = tester.run(f"--from-file {answers} --profile-name globalci")

        assert rc == 0
        saved = _load_profile(config_paths, "globalci")
        regions = saved["allowed_bedrock_regions"]
        assert regions
        assert all(not r.startswith("all-") for r in regions)
        # Parity: identical to what the interactive path assigns for the same selection.
        assert regions == expand_region_sentinels(get_destination_regions_for_model_profile("sonnet-4-6", "global"))

    def test_explicit_sentinel_accepted_and_expanded(self, config_paths, tester, tmp_path):
        """An explicit ["all-commercial"] no longer fails validation (interactive parity)."""
        answers = tmp_path / "answers.yaml"
        answers.write_text(EXPLICIT_SENTINEL_YAML)

        rc = tester.run(f"--from-file {answers} --profile-name sentinelci")

        assert rc == 0
        saved = _load_profile(config_paths, "sentinelci")
        regions = saved["allowed_bedrock_regions"]
        assert regions
        assert "all-commercial" not in regions
        assert all(validate_aws_region(r) for r in regions)


class TestRenderedParamCorrectness:
    """The values deploy renders/consumes are real regions, not sentinels."""

    def test_auth_stack_param_renders_real_regions(self, config_paths, tester, tmp_path):
        answers = tmp_path / "answers.yaml"
        answers.write_text(GLOBAL_YAML)
        assert tester.run(f"--from-file {answers} --profile-name paramci") == 0
        saved = _load_profile(config_paths, "paramci")

        # deploy.py renders AllowedBedrockRegions={','.join(profile.allowed_bedrock_regions)}
        rendered = ",".join(saved["allowed_bedrock_regions"])
        assert "all-" not in rendered
        assert all(validate_aws_region(r) for r in rendered.split(","))

    def test_metering_loop_regions_are_boto3_valid(self, config_paths, tester, tmp_path):
        """The metering per-region loop builds boto3 clients from this list."""
        answers = tmp_path / "answers.yaml"
        answers.write_text(GLOBAL_YAML)
        assert tester.run(f"--from-file {answers} --profile-name meterci") == 0
        saved = _load_profile(config_paths, "meterci")

        for region in saved["allowed_bedrock_regions"]:
            assert validate_aws_region(region), f"metering would build a boto3 client for {region!r}"
