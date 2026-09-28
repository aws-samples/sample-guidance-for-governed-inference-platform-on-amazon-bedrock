# ABOUTME: Tests for cost-based quota enforcement (pricing utility + enforcement logic)
# ABOUTME: Validates cost calculation across model families and enforcement decisions

"""Tests for cost-based quota enforcement."""

import json
import sys
from pathlib import Path

import pytest
import yaml

# Add shared to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "deployment" / "infrastructure" / "lambda-functions"))

from shared.pricing import (
    DEFAULT_RATES,
    LEGACY_MODEL_PRICING,
    calculate_cost,
    get_rates,
    reset_pricing_warnings,
    resolve_model_family,
)


class _CfnLoader(yaml.SafeLoader):
    pass


def _cfn_tag_constructor(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return None


_CfnLoader.add_multi_constructor("!", _cfn_tag_constructor)


def _load_metering_template() -> dict:
    path = Path(__file__).parents[2] / "deployment" / "infrastructure" / "quota-metering.yaml"
    with open(path, encoding="utf-8") as template_file:
        return yaml.load(template_file, Loader=_CfnLoader)  # nosec B506


def _tag_dict(resource: dict) -> dict[str, str]:
    return {tag["Key"]: tag["Value"] for tag in resource["Properties"]["Tags"]}


class TestPricingUtility:
    """Tests for shared/pricing.py."""

    def test_sonnet_input_cost(self):
        """1M input tokens at Sonnet rates = $3.00."""
        cost = calculate_cost(1_000_000, 0, 0, 0, "sonnet")
        assert cost == pytest.approx(3.0)

    def test_sonnet_output_cost(self):
        """1M output tokens at Sonnet rates = $15.00."""
        cost = calculate_cost(0, 1_000_000, 0, 0, "sonnet")
        assert cost == pytest.approx(15.0)

    def test_sonnet_cache_read_cost(self):
        """1M cache read tokens at Sonnet rates = $0.30."""
        cost = calculate_cost(0, 0, 1_000_000, 0, "sonnet")
        assert cost == pytest.approx(0.3)

    def test_opus_input_cost(self):
        """1M input tokens at Opus rates = $5.00."""
        cost = calculate_cost(1_000_000, 0, 0, 0, "opus")
        assert cost == pytest.approx(5.0)

    def test_fable_input_cost(self):
        """1M input tokens at Fable rates = $10.00."""
        cost = calculate_cost(1_000_000, 0, 0, 0, "fable")
        assert cost == pytest.approx(10.0)

    def test_haiku_input_cost(self):
        """1M input tokens at Haiku rates = $1.00."""
        cost = calculate_cost(1_000_000, 0, 0, 0, "haiku")
        assert cost == pytest.approx(1.0)

    def test_mixed_tokens_sonnet(self):
        """Realistic mix: 500K input + 200K output + 10M cache reads."""
        cost = calculate_cost(500_000, 200_000, 10_000_000, 0, "sonnet")
        expected = (0.5 * 3.0) + (0.2 * 15.0) + (10.0 * 0.3)
        assert cost == pytest.approx(expected)  # $1.50 + $3.00 + $3.00 = $7.50

    def test_unknown_model_defaults_to_sonnet(self):
        """Unknown model family falls back to Sonnet rates."""
        cost = calculate_cost(1_000_000, 0, 0, 0, "unknown_model")
        assert cost == pytest.approx(3.0)

    def test_zero_tokens_zero_cost(self):
        """No tokens = no cost."""
        assert calculate_cost(0, 0, 0, 0) == 0.0


class TestModelResolution:
    """Tests for resolve_model_family."""

    def test_sonnet_cris(self):
        assert resolve_model_family("us.anthropic.claude-sonnet-4-6-v1") == "sonnet"

    def test_opus_cris(self):
        assert resolve_model_family("global.anthropic.claude-opus-4-7") == "opus"

    def test_haiku_cris(self):
        assert resolve_model_family("eu.anthropic.claude-haiku-4-5-20251001-v1:0") == "haiku"

    def test_fable_cris(self):
        assert resolve_model_family("us.anthropic.claude-fable-5") == "fable"

    def test_unknown_defaults_to_sonnet(self):
        assert resolve_model_family("some-random-model") == "sonnet"

    def test_empty_string(self):
        assert resolve_model_family("") == "sonnet"


class TestPricingOverride:
    """Tests for BEDROCK_PRICING_RATES_JSON env var override."""

    def test_override_merges_with_defaults(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_PRICING_RATES_JSON", '{"sonnet": {"input": 4.00}}')
        rates = get_rates()
        assert rates["sonnet"]["input"] == 4.00
        assert rates["sonnet"]["output"] == 15.00  # unchanged

    def test_invalid_json_uses_defaults(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_PRICING_RATES_JSON", "not valid json")
        rates = get_rates()
        assert rates["sonnet"]["input"] == 3.00

    def test_empty_env_uses_defaults(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_PRICING_RATES_JSON", "")
        rates = get_rates()
        assert rates == {k: dict(v) for k, v in DEFAULT_RATES.items()}


class TestLegacyPremiumPricing:
    """Legacy / public-extended-access rate selection (shared/pricing.py).

    Bedrock's provider-set extended-access premium is per-model and date-based
    (docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle.html). These
    tests pin: verified rates apply once the date passes, unverified premiums
    fall back to standard rates WITH a structured warning (A5: degrade visibly,
    never silently, never with invented numbers), and active models regress
    nothing.
    """

    @pytest.fixture(autouse=True)
    def _clean_warnings(self):
        reset_pricing_warnings()
        yield
        reset_pricing_warnings()

    def _warnings(self, capsys):
        return [json.loads(line) for line in capsys.readouterr().out.strip().splitlines() if line.strip()]

    # --- Opus 4 / 4.1: standard rates are $15/$75, not the $5/$25 opus family ---

    def test_opus_4_1_resolves_to_opus_legacy(self):
        assert resolve_model_family("us.anthropic.claude-opus-4-1-20250805-v1:0") == "opus-legacy"

    def test_opus_4_resolves_to_opus_legacy(self):
        assert resolve_model_family("us.anthropic.claude-opus-4-20250514-v1:0") == "opus-legacy"

    def test_opus_legacy_input_cost(self):
        """1M input tokens at Opus 4.x legacy-generation rates = $15.00 (was $5.00 via 'opus')."""
        assert calculate_cost(1_000_000, 0, 0, 0, "opus-legacy") == pytest.approx(15.0)

    def test_opus_legacy_full_rate_card(self):
        assert DEFAULT_RATES["opus-legacy"] == {
            "input": 15.00,
            "output": 75.00,
            "cache_read": 1.50,
            "cache_write": 18.75,
        }

    def test_opus_4_past_eol_warns(self, capsys):
        resolve_model_family("us.anthropic.claude-opus-4-20250514-v1:0")
        events = self._warnings(capsys)
        assert len(events) == 1
        assert events[0]["event"] == "legacy_model_presumed_eol"
        assert events[0]["level"] == "WARNING"

    # --- Claude 3.5 Sonnet v1/v2: published extended-access rate (2x standard) ---

    def test_sonnet_3_5_v2_past_extended_date_uses_published_premium(self):
        family = resolve_model_family("us.anthropic.claude-3-5-sonnet-20241022-v2:0", now="2026-07-29")
        assert family == "sonnet-3-5-extended"
        assert calculate_cost(1_000_000, 0, 0, 0, family) == pytest.approx(6.0)

    def test_sonnet_3_5_v1_past_extended_date_uses_published_premium(self):
        assert (
            resolve_model_family("anthropic.claude-3-5-sonnet-20240620-v1:0", now="2026-07-29") == "sonnet-3-5-extended"
        )

    def test_sonnet_3_5_before_extended_date_uses_standard(self, capsys):
        """Before the pricing-page effective date (2025-12-01) the standard rate applies, silently."""
        assert resolve_model_family("us.anthropic.claude-3-5-sonnet-20241022-v2:0", now="2025-11-30") == "sonnet"
        assert self._warnings(capsys) == []

    def test_sonnet_3_5_extended_rate_card_is_2x_standard(self):
        assert DEFAULT_RATES["sonnet-3-5-extended"] == {
            "input": 6.00,
            "output": 30.00,
            "cache_read": 0.60,
            "cache_write": 7.50,
        }

    # --- Sonnet 4: extended access ACTIVE since 2026-07-14, premium NOT published ---

    def test_sonnet_4_past_extended_date_stays_standard_but_warns(self, capsys):
        family = resolve_model_family("us.anthropic.claude-sonnet-4-20250514-v1:0", now="2026-07-29")
        assert family == "sonnet"  # never an invented premium
        events = self._warnings(capsys)
        assert len(events) == 1
        assert events[0]["event"] == "unpriced_extended_access"
        assert events[0]["level"] == "WARNING"
        assert events[0]["model"] == "Claude Sonnet 4"
        assert events[0]["override_rate_key"] == "sonnet-4-extended"
        assert events[0]["extended_access_date"] == "2026-07-14"

    def test_sonnet_4_before_extended_date_standard_no_warning(self, capsys):
        assert resolve_model_family("us.anthropic.claude-sonnet-4-20250514-v1:0", now="2026-07-13") == "sonnet"
        assert self._warnings(capsys) == []

    def test_sonnet_4_warning_dedupes_per_model(self, capsys):
        for _ in range(3):
            resolve_model_family("us.anthropic.claude-sonnet-4-20250514-v1:0", now="2026-07-29")
        assert len(self._warnings(capsys)) == 1

    def test_sonnet_4_admin_override_prices_extended_access(self, monkeypatch, capsys):
        """The AWS Health-notified premium can be supplied via env override — then no warning."""
        monkeypatch.setenv(
            "BEDROCK_PRICING_RATES_JSON",
            '{"sonnet-4-extended": {"input": 6.00, "output": 30.00, "cache_read": 0.60, "cache_write": 7.50}}',
        )
        family = resolve_model_family("us.anthropic.claude-sonnet-4-20250514-v1:0", now="2026-07-29")
        assert family == "sonnet-4-extended"
        assert self._warnings(capsys) == []
        assert calculate_cost(1_000_000, 0, 0, 0, family, rates=get_rates()) == pytest.approx(6.0)

    # --- Opus 4.1: legacy now, extended access scheduled 2026-10-08 ---

    def test_opus_4_1_before_extended_date_uses_own_standard_rates(self, capsys):
        assert resolve_model_family("us.anthropic.claude-opus-4-1-20250805-v1:0", now="2026-07-29") == "opus-legacy"
        assert self._warnings(capsys) == []

    def test_opus_4_1_past_extended_date_warns_unpriced(self, capsys):
        family = resolve_model_family("us.anthropic.claude-opus-4-1-20250805-v1:0", now="2026-10-08")
        assert family == "opus-legacy"
        events = self._warnings(capsys)
        assert len(events) == 1
        assert events[0]["event"] == "unpriced_extended_access"
        assert events[0]["override_rate_key"] == "opus-4-1-extended"

    # --- Sonnet 3.7 (GovCloud) and Claude 3 Haiku: extended access active, unpriced ---

    def test_sonnet_3_7_warns_unpriced(self, capsys):
        family = resolve_model_family("us-gov.anthropic.claude-3-7-sonnet-20250219-v1:0", now="2026-07-29")
        assert family == "sonnet"
        assert self._warnings(capsys)[0]["override_rate_key"] == "sonnet-3-7-extended"

    def test_claude_3_haiku_warns_unpriced_and_keeps_conservative_family(self, capsys):
        family = resolve_model_family("anthropic.claude-3-haiku-20240307-v1:0", now="2026-07-29")
        assert family == "haiku"  # $1/$5 family exceeds the model's own $0.25/$1.25 — conservative
        assert self._warnings(capsys)[0]["override_rate_key"] == "haiku-3-extended"

    # --- No regression on active models ---

    def test_active_models_do_not_match_legacy_entries(self, capsys):
        assert resolve_model_family("us.anthropic.claude-sonnet-4-5-20250929-v1:0") == "sonnet"
        assert resolve_model_family("us.anthropic.claude-sonnet-4-6-v1") == "sonnet"
        assert resolve_model_family("global.anthropic.claude-sonnet-5") == "sonnet"
        assert resolve_model_family("us.anthropic.claude-opus-4-5-20251101-v1:0") == "opus"
        assert resolve_model_family("us.anthropic.claude-opus-4-8") == "opus"
        assert resolve_model_family("eu.anthropic.claude-haiku-4-5-20251001-v1:0") == "haiku"
        assert resolve_model_family("us.anthropic.claude-fable-5") == "fable"
        assert self._warnings(capsys) == []

    def test_standard_rate_families_unchanged(self):
        assert DEFAULT_RATES["sonnet"] == {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75}
        assert DEFAULT_RATES["opus"] == {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25}
        assert DEFAULT_RATES["haiku"] == {"input": 1.00, "output": 5.00, "cache_read": 0.10, "cache_write": 1.25}
        assert DEFAULT_RATES["fable"] == {"input": 10.00, "output": 50.00, "cache_read": 1.00, "cache_write": 12.50}

    def test_every_legacy_entry_standard_family_has_rates(self):
        """Structural invariant: no legacy entry can point at a missing standard family."""
        for entry in LEGACY_MODEL_PRICING:
            assert entry["standard_family"] in DEFAULT_RATES, entry["label"]

    def test_aip_still_wins_over_legacy_matching(self):
        arn = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/claude-sonnet-4-20250514"
        assert resolve_model_family(arn) == "unpriced_aip"


class TestCostEnforcementLogic:
    """Tests for quota_check cost enforcement decisions."""

    def _simulate_enforcement(self, usage: dict, policy: dict) -> dict:
        """Simulate the quota_check enforcement logic for cost."""
        monthly_cost = float(usage.get("cost_usd", 0))
        daily_cost = float(usage.get("daily_cost_usd", 0))
        monthly_cost_limit = float(policy.get("monthly_cost_limit", 0))
        daily_cost_limit = float(policy.get("daily_cost_limit", 0))

        if monthly_cost_limit > 0 and monthly_cost >= monthly_cost_limit:
            return {"allowed": False, "reason": "monthly_cost_exceeded"}
        if daily_cost_limit > 0 and daily_cost >= daily_cost_limit:
            return {"allowed": False, "reason": "daily_cost_exceeded"}
        return {"allowed": True, "reason": "within_budget"}

    def test_within_budget_allowed(self):
        result = self._simulate_enforcement(
            {"cost_usd": 30.0, "daily_cost_usd": 5.0}, {"monthly_cost_limit": 50.0, "daily_cost_limit": 10.0}
        )
        assert result["allowed"] is True

    def test_monthly_exceeded_blocked(self):
        result = self._simulate_enforcement(
            {"cost_usd": 55.0, "daily_cost_usd": 5.0}, {"monthly_cost_limit": 50.0, "daily_cost_limit": 10.0}
        )
        assert result["allowed"] is False
        assert result["reason"] == "monthly_cost_exceeded"

    def test_daily_exceeded_blocked(self):
        result = self._simulate_enforcement(
            {"cost_usd": 30.0, "daily_cost_usd": 12.0}, {"monthly_cost_limit": 50.0, "daily_cost_limit": 10.0}
        )
        assert result["allowed"] is False
        assert result["reason"] == "daily_cost_exceeded"

    def test_no_cost_limit_allows(self):
        """When no cost limit configured, cost enforcement is skipped."""
        result = self._simulate_enforcement(
            {"cost_usd": 999.0},
            {"monthly_cost_limit": 0},  # disabled
        )
        assert result["allowed"] is True

    def test_missing_cost_data_allows(self):
        """If cost_usd not in usage (old data), enforcement passes."""
        result = self._simulate_enforcement(
            {"total_tokens": 5000000},  # no cost_usd field
            {"monthly_cost_limit": 50.0},
        )
        assert result["allowed"] is True


class TestMeteringCellAttribution:
    def test_metering_resources_are_service_and_cell_tagged(self):
        template = _load_metering_template()
        for logical_id in (
            "MeteringLogGroup",
            "MeteringConfigFunction",
            "MeteringProcessorDLQ",
            "MeteringProcessorFunction",
        ):
            tags = _tag_dict(template["Resources"][logical_id])
            assert tags["gip:service"] == "quota-metering"
            assert tags["gip:cell"] == "${AWS::AccountId}:${AWS::Region}"

    def test_metering_outputs_account_local_cell_identity(self):
        template = _load_metering_template()
        assert template["Outputs"]["InferenceCellId"]["Value"] == "${AWS::AccountId}:${AWS::Region}"
        assert template["Outputs"]["MeteringServiceName"]["Value"] == "quota-metering"
