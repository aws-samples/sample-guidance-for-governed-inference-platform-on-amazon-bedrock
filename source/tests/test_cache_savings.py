# ABOUTME: Tests for prompt-cache savings visibility (calculate_cache_savings + dashboard/Athena contracts)
# ABOUTME: Validates the savings formula, negative-savings honesty, div0 guard, and widget/query wiring

"""Tests for prompt-cache savings metrics."""

import json
import sys
from pathlib import Path

import pytest
import yaml

# Add shared to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "deployment" / "infrastructure" / "lambda-functions"))

from shared.pricing import DEFAULT_RATES, calculate_cache_savings, calculate_cost

INFRA_DIR = Path(__file__).parent.parent.parent / "deployment" / "infrastructure"


class TestCalculateCacheSavings:
    """Tests for shared/pricing.py calculate_cache_savings."""

    def test_sonnet_pure_cache_read_exact(self):
        """1M cache reads on Sonnet: savings = 1.0 * (3.00 - 0.30) = $2.70."""
        result = calculate_cache_savings(0, 0, 1_000_000, 0, "sonnet")
        assert result["savings_usd"] == pytest.approx(2.70)
        assert result["hypothetical_cost"] == pytest.approx(3.00)
        assert result["actual_cost"] == pytest.approx(0.30)
        assert result["cache_hit_rate"] == pytest.approx(1.0)

    def test_sonnet_read_minus_write_penalty(self):
        """1M reads + 1M writes on Sonnet: 2.70 read savings - 0.75 write penalty = $1.95."""
        result = calculate_cache_savings(0, 0, 1_000_000, 1_000_000, "sonnet")
        assert result["savings_usd"] == pytest.approx(1.95)
        # hypothetical: 2M input-side tokens at $3.00/MTok
        assert result["hypothetical_cost"] == pytest.approx(6.00)
        # actual: 1M at $0.30 + 1M at $3.75
        assert result["actual_cost"] == pytest.approx(4.05)

    def test_negative_savings_write_heavy(self):
        """Write-heavy, zero-reuse workload costs MORE than no caching (honest negative)."""
        result = calculate_cache_savings(0, 0, 0, 1_000_000, "sonnet")
        assert result["savings_usd"] == pytest.approx(-0.75)
        assert result["savings_usd"] < 0
        assert result["hypothetical_cost"] == pytest.approx(3.00)
        assert result["actual_cost"] == pytest.approx(3.75)

    def test_zero_division_guard(self):
        """No input-side tokens: cache_hit_rate is 0.0, not a ZeroDivisionError."""
        result = calculate_cache_savings(0, 0, 0, 0, "sonnet")
        assert result["cache_hit_rate"] == 0.0
        assert result["savings_usd"] == 0.0
        assert result["hypothetical_cost"] == 0.0
        assert result["actual_cost"] == 0.0

    def test_output_only_has_no_cache_hit_rate(self):
        """Output tokens alone must not trip the div0 guard either."""
        result = calculate_cache_savings(0, 1_000_000, 0, 0, "sonnet")
        assert result["cache_hit_rate"] == 0.0
        assert result["savings_usd"] == pytest.approx(0.0)

    def test_cache_hit_rate_formula(self):
        """hit rate = cache_read / (input + cache_read); output/write excluded."""
        result = calculate_cache_savings(1_000_000, 5_000_000, 3_000_000, 2_000_000, "sonnet")
        assert result["cache_hit_rate"] == pytest.approx(0.75)

    def test_unknown_family_falls_back_to_sonnet(self):
        """Unknown model family uses Sonnet (DEFAULT_FAMILY) rates."""
        unknown = calculate_cache_savings(0, 0, 1_000_000, 0, "unknown_model")
        sonnet = calculate_cache_savings(0, 0, 1_000_000, 0, "sonnet")
        assert unknown == sonnet

    def test_fable_exact(self):
        """1M cache reads on Fable: savings = 10.00 - 1.00 = $9.00."""
        result = calculate_cache_savings(0, 0, 1_000_000, 0, "fable")
        assert result["savings_usd"] == pytest.approx(9.00)

    def test_opus_exact(self):
        """1M cache reads on Opus: savings = 5.00 - 0.50 = $4.50."""
        result = calculate_cache_savings(0, 0, 1_000_000, 0, "opus")
        assert result["savings_usd"] == pytest.approx(4.50)

    def test_haiku_exact(self):
        """1M cache reads on Haiku: savings = 1.00 - 0.10 = $0.90."""
        result = calculate_cache_savings(0, 0, 1_000_000, 0, "haiku")
        assert result["savings_usd"] == pytest.approx(0.90)

    def test_actual_cost_matches_calculate_cost(self):
        """actual_cost must agree with the existing calculate_cost helper."""
        args = (500_000, 200_000, 10_000_000, 300_000)
        result = calculate_cache_savings(*args, model_family="opus")
        assert result["actual_cost"] == pytest.approx(calculate_cost(*args, model_family="opus"))

    def test_savings_is_hypothetical_minus_actual(self):
        """Identity: savings_usd == hypothetical_cost - actual_cost."""
        result = calculate_cache_savings(2_000_000, 1_000_000, 8_000_000, 4_000_000, "fable")
        assert result["savings_usd"] == pytest.approx(result["hypothetical_cost"] - result["actual_cost"])

    def test_output_tokens_do_not_affect_savings(self):
        """Output is billed identically with/without caching — savings unchanged."""
        with_output = calculate_cache_savings(1_000_000, 9_000_000, 2_000_000, 500_000, "sonnet")
        without_output = calculate_cache_savings(1_000_000, 0, 2_000_000, 500_000, "sonnet")
        assert with_output["savings_usd"] == pytest.approx(without_output["savings_usd"])

    def test_custom_rates_override(self):
        """Explicit rates dict takes precedence over defaults."""
        rates = {"sonnet": {"input": 10.0, "output": 20.0, "cache_read": 1.0, "cache_write": 12.0}}
        result = calculate_cache_savings(0, 0, 1_000_000, 0, "sonnet", rates=rates)
        assert result["savings_usd"] == pytest.approx(9.0)

    def test_default_rates_unchanged(self):
        """Savings math relies on these published rates; fail loudly if they drift."""
        assert DEFAULT_RATES["sonnet"] == {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75}


# CloudFormation-aware YAML loader (handles !Ref, !Sub, !GetAtt, etc.)
class CFNLoader(yaml.SafeLoader):
    pass


def _cfn_constructor(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    elif isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return None


CFNLoader.add_multi_constructor("!", _cfn_constructor)


def _load_template(name: str) -> dict:
    path = INFRA_DIR / name
    if not path.exists():
        pytest.skip(f"Template {name} not found")
    with open(path, encoding="utf-8") as f:
        return yaml.load(f, Loader=CFNLoader)  # nosec B506


def _dashboard_widgets(template: dict) -> list[dict]:
    """Parse the DashboardBody JSON (after !Sub placeholder substitution)."""
    body = template["Resources"]["Dashboard"]["Properties"]["DashboardBody"]
    body = body.replace("${MetricsRegion}", "us-east-1")
    return json.loads(body)["widgets"]


class TestClaudeCodeDashboardCacheWidgets:
    """Contract: claude-code-dashboard.yaml exposes the Prompt Cache section."""

    @pytest.fixture()
    def widgets(self):
        return _dashboard_widgets(_load_template("claude-code-dashboard.yaml"))

    def _titles(self, widgets):
        return [w["properties"].get("title", "") for w in widgets]

    def test_dashboard_body_is_valid_json(self, widgets):
        assert len(widgets) > 0

    def test_has_prompt_cache_section_header(self, widgets):
        markdowns = [w["properties"].get("markdown", "") for w in widgets if w["type"] == "text"]
        assert any("Prompt Cache" in m for m in markdowns)

    def test_has_org_cache_hit_rate_widget(self, widgets):
        titles = self._titles(widgets)
        assert any(t.startswith("Org Cache Hit Rate") for t in titles)

    def test_savings_widget_labeled_as_sonnet_estimate(self, widgets):
        """Blended-rate approximation must be disclosed in the widget title."""
        titles = self._titles(widgets)
        assert any("estimate (Sonnet-family rates)" in t for t in titles)

    def test_savings_widget_uses_sonnet_cache_deltas(self, widgets):
        """Savings math: cacheRead * (3.00-0.30) - cacheCreation * (3.75-3.00), per MTok."""
        savings = [w for w in widgets if "estimate (Sonnet-family rates)" in w["properties"].get("title", "")]
        assert savings, "savings widget missing"
        query = savings[0]["properties"]["data"]["queries"][0]["query"]
        assert "* 2.7" in query
        assert "* 0.75" in query
        assert "/ 1000000" in query
        assert 'type="cacheRead"' in query
        assert 'type="cacheCreation"' in query

    def test_org_hit_rate_uses_input_plus_cache_read_denominator(self, widgets):
        hit_rate = [w for w in widgets if w["properties"].get("title", "").startswith("Org Cache Hit Rate")]
        assert hit_rate, "org cache hit rate widget missing"
        query = hit_rate[0]["properties"]["data"]["queries"][0]["query"]
        assert 'type="cacheRead"' in query
        assert 'type="input"' in query
        assert "* 100" in query

    def test_has_cache_tokens_by_user_widget(self, widgets):
        by_user = [w for w in widgets if "Cache Read vs Write Tokens by User" in w["properties"].get("title", "")]
        assert by_user, "cache read vs write by user widget missing"
        queries = [q["query"] for q in by_user[0]["properties"]["data"]["queries"]]
        assert any('type="cacheRead"' in q and "topk(10" in q for q in queries)
        assert any('type="cacheCreation"' in q and "topk(10" in q for q in queries)

    def test_has_cache_hit_rate_by_user_widget(self, widgets):
        by_user = [w for w in widgets if w["properties"].get("title", "").startswith("Cache Hit Rate by User")]
        assert by_user, "cache hit rate by user widget missing"
        query = by_user[0]["properties"]["data"]["queries"][0]["query"]
        assert 'sum by ("user.email")' in query
        assert 'type="cacheRead"' in query
        assert 'type="input"' in query


class TestCoWorkDashboardCacheWidgets:
    """Contract: cowork-dashboard.yaml exposes the org-wide Prompt Cache widgets."""

    @pytest.fixture()
    def widgets(self):
        return _dashboard_widgets(_load_template("cowork-dashboard.yaml"))

    def test_dashboard_body_is_valid_json(self, widgets):
        assert len(widgets) > 0

    def test_has_org_cache_hit_rate_widget(self, widgets):
        hit_rate = [w for w in widgets if w["properties"].get("title", "").startswith("Org Cache Hit Rate")]
        assert hit_rate, "org cache hit rate widget missing"
        query = hit_rate[0]["properties"]["data"]["queries"][0]["query"]
        assert 'MetricName="token.usage.cache_read"' in query
        assert 'MetricName="token.usage.input"' in query

    def test_has_savings_widget_labeled_as_sonnet_estimate(self, widgets):
        savings = [w for w in widgets if "estimate (Sonnet-family rates)" in w["properties"].get("title", "")]
        assert savings, "savings widget missing"
        query = savings[0]["properties"]["data"]["queries"][0]["query"]
        assert "* 2.7" in query
        assert "* 0.75" in query
        assert 'MetricName="token.usage.cache_creation"' in query


class TestAthenaCacheSavingsQueries:
    """Contract: analytics-pipeline.yaml ships exact per-family cache savings queries."""

    @pytest.fixture()
    def resources(self):
        return _load_template("analytics-pipeline.yaml")["Resources"]

    @pytest.mark.parametrize(
        ("resource", "name", "group_col"),
        [
            ("CacheSavingsByUserQuery", "CacheSavingsByUser", "user_email"),
            ("CacheSavingsByModelQuery", "CacheSavingsByModel", "model"),
        ],
    )
    def test_query_exists_and_computes_savings(self, resources, resource, name, group_col):
        assert resource in resources, f"{resource} named query missing"
        props = resources[resource]["Properties"]
        assert props["Name"] == name
        query = props["QueryString"]
        assert f"GROUP BY {group_col}" in query
        # Exact per-family cache rates (actual cost)
        assert "WHEN 'cacheRead' THEN CASE WHEN model LIKE '%fable%' THEN 1.00" in query
        assert "WHEN 'cacheCreation' THEN CASE WHEN model LIKE '%fable%' THEN 12.50" in query
        # Hypothetical cost bills cache tokens at the family INPUT rate
        assert "WHEN 'cacheRead' THEN CASE WHEN model LIKE '%fable%' THEN 10.00" in query
        assert "WHEN 'cacheCreation' THEN CASE WHEN model LIKE '%fable%' THEN 10.00" in query
        # Output columns
        for col in ("savings_usd", "actual_cost_usd", "hypothetical_cost_usd", "cache_hit_rate_pct"):
            assert col in query, f"{resource} missing column {col}"
        # Division-by-zero guard on hit rate
        assert "NULLIF(input_tokens + cache_read_tokens, 0)" in query
