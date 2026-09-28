# ABOUTME: Pure-function tests for cli/utils/residency.py — region geography mapping and warnings
# ABOUTME: Covers each warning trigger (infra, web search, codebuild) and the no-warning happy path

"""Tests for governed_inference_platform.cli.utils.residency."""

import pytest

from governed_inference_platform.cli.utils.residency import (
    DATA_RESIDENCY_GEOGRAPHIES,
    region_geography,
    residency_warnings,
)


class TestRegionGeography:
    """Prefix-based mapping with au/jp special cases."""

    @pytest.mark.parametrize(
        ("region", "expected"),
        [
            ("us-east-1", "us"),
            ("us-west-2", "us"),
            ("us-gov-west-1", "us-gov"),
            ("us-gov-east-1", "us-gov"),
            ("eu-west-1", "eu"),
            ("eu-central-1", "eu"),
            ("eu-north-1", "eu"),
            ("ap-south-1", "apac"),
            ("ap-southeast-1", "apac"),
            ("ca-central-1", "ca"),
            ("sa-east-1", "sa"),
            ("me-south-1", "me"),
            ("af-south-1", "af"),
        ],
    )
    def test_prefix_mapping(self, region, expected):
        assert region_geography(region) == expected

    @pytest.mark.parametrize(
        ("region", "expected"),
        [
            ("ap-southeast-2", "au"),  # Sydney
            ("ap-southeast-4", "au"),  # Melbourne
            ("ap-northeast-1", "jp"),  # Tokyo
            ("ap-northeast-3", "jp"),  # Osaka
        ],
    )
    def test_au_jp_special_cases(self, region, expected):
        assert region_geography(region) == expected

    def test_ap_northeast_2_is_apac_not_jp(self):
        """Seoul is plain apac — only Tokyo/Osaka are jp."""
        assert region_geography("ap-northeast-2") == "apac"

    def test_unknown_prefix_falls_back_to_first_token(self):
        assert region_geography("il-central-1") == "il"

    def test_empty_and_none_safe(self):
        assert region_geography("") == ""
        assert region_geography(None) == ""

    def test_case_and_whitespace_normalized(self):
        assert region_geography("  EU-WEST-1 ") == "eu"


class TestResidencyWarnings:
    """Each trigger plus the happy path."""

    def test_happy_path_eu_infra_eu_cris_no_websearch(self):
        assert residency_warnings("eu-west-1", "eu", web_search_enabled=False) == []

    def test_happy_path_with_eu_codebuild(self):
        assert residency_warnings("eu-west-1", "eu", False, codebuild_region="eu-central-1") == []

    def test_infra_outside_eu_geography_warns(self):
        warnings = residency_warnings("us-east-1", "eu", web_search_enabled=False)
        assert len(warnings) == 1
        assert "us-east-1" in warnings[0]
        assert "user emails" in warnings[0]
        assert "quota DynamoDB" in warnings[0]

    def test_infra_outside_au_geography_warns(self):
        warnings = residency_warnings("ap-southeast-1", "au", web_search_enabled=False)
        assert len(warnings) == 1
        assert "'au'" in warnings[0]

    def test_infra_matching_jp_geography_no_warning(self):
        assert residency_warnings("ap-northeast-1", "jp", web_search_enabled=False) == []

    def test_web_search_with_eu_cris_warns(self):
        warnings = residency_warnings("eu-west-1", "eu", web_search_enabled=True)
        assert len(warnings) == 1
        assert "us-east-1" in warnings[0]
        assert "bedrock-agentcore-gateway.yaml" in warnings[0]

    def test_memory_with_eu_cris_warns_about_content(self):
        warnings = residency_warnings("eu-west-1", "eu", web_search_enabled=True, memory_enabled=True)
        assert len(warnings) == 2
        assert "AgentCore Memory content" in warnings[1]
        assert "conversation-derived" in warnings[1]
        assert "us-east-1" in warnings[1]
        assert "memory-stack.yaml" in warnings[1]

    def test_codebuild_outside_geography_warns(self):
        warnings = residency_warnings("eu-west-1", "eu", False, codebuild_region="us-east-1")
        assert len(warnings) == 1
        assert "CodeBuild" in warnings[0]
        assert "us-east-1" in warnings[0]

    def test_codebuild_none_is_ignored(self):
        assert residency_warnings("eu-west-1", "eu", False, codebuild_region=None) == []

    def test_all_four_triggers_stack(self):
        warnings = residency_warnings("us-east-1", "eu", True, codebuild_region="us-west-2", memory_enabled=True)
        assert len(warnings) == 4

    def test_us_cris_never_warns(self):
        assert residency_warnings("eu-west-1", "us", True, codebuild_region="ap-south-1") == []

    def test_global_cris_never_warns(self):
        assert residency_warnings("us-east-1", "global", True) == []

    def test_apac_cris_never_warns(self):
        """apac has no strict residency requirement (mirrors DATA_RESIDENCY_PREFIXES)."""
        assert residency_warnings("us-east-1", "apac", True) == []

    def test_legacy_europe_alias_normalized(self):
        """Older profiles store 'europe' — must behave exactly like 'eu'."""
        warnings = residency_warnings("us-east-1", "europe", web_search_enabled=False)
        assert len(warnings) == 1
        assert "'eu'" in warnings[0]

    def test_legacy_japan_alias_normalized(self):
        assert residency_warnings("ap-northeast-1", "japan", web_search_enabled=False) == []

    def test_set_matches_models_data_residency_prefixes(self):
        from governed_inference_platform.models import DATA_RESIDENCY_PREFIXES

        assert DATA_RESIDENCY_GEOGRAPHIES == DATA_RESIDENCY_PREFIXES
