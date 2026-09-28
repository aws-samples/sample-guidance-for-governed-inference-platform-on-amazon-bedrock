# ABOUTME: Tests for the quota_monitor Lambda's stale-day guard on the daily counter
# ABOUTME: Idle users whose daily_date is not today must not re-alert as "daily exceeded"

"""Tests for quota_monitor daily stale-day reset in the threshold/alert step.

Regression: an idle user's daily_tokens froze above the daily limit and a fresh
"Daily Token Quota EXCEEDED" alert went out every new UTC day, because the
threshold step read daily_tokens verbatim (without the stale-day guard that
quota_check already applies).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

LAMBDA_PATH = (
    Path(__file__).resolve().parents[2]
    / "deployment"
    / "infrastructure"
    / "lambda-functions"
    / "quota_monitor"
    / "index.py"
)


def _load_quota_monitor(env: dict) -> object:
    """Load the quota_monitor Lambda module fresh with the given environment."""
    for key, value in env.items():
        os.environ[key] = value

    module_name = f"quota_monitor_index_{id(env)}"
    spec = importlib.util.spec_from_file_location(module_name, LAMBDA_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def base_env():
    return {
        "QUOTA_TABLE": "TestQuotaTable",
        "POLICIES_TABLE": "TestPoliciesTable",
        "SNS_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:test-alerts",
        "ENABLE_FINEGRAINED_QUOTAS": "false",
        "MONTHLY_TOKEN_LIMIT": "40000000",
        # Explicit zeros: os.environ persists across module loads in this file,
        # so every env dict must pin the cost vars to stay order-independent.
        "MONTHLY_COST_LIMIT_USD": "0",
        "DAILY_COST_LIMIT_USD": "0",
    }


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _yesterday() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")


def _scan_response(items: list[dict]) -> dict:
    """A single-page DynamoDB scan response (no LastEvaluatedKey)."""
    return {"Items": items}


def _patch_monitor(mod, scan_item: dict, daily_token_limit: int = 2_000_000):
    """Wire the monitor so it skips PromQL/update and scans a single user row.

    Returns the MagicMock SNS client for assertions on published alerts.
    """
    # No new activity -> update step is a no-op and daily reset never happens
    # via update_quota_metrics (this is the idle-user condition).
    mod.fetch_usage_from_promql = MagicMock(return_value={})

    mod.quota_table = MagicMock()
    mod.quota_table.scan.return_value = _scan_response([scan_item])
    # get_sent_alerts issues a query; return no prior alerts.
    mod.quota_table.query.return_value = {"Items": []}

    # Force the env-var policy to carry a daily limit so daily checks run.
    base_policy = {
        "policy_type": "default",
        "identifier": "environment",
        "monthly_token_limit": 40_000_000,
        "daily_token_limit": daily_token_limit,
        "warning_threshold_80": 32_000_000,
        "warning_threshold_90": 36_000_000,
        "enforcement_mode": "alert",
        "enabled": True,
    }
    mod.resolve_user_quota = MagicMock(return_value=base_policy)

    mod.sns_client = MagicMock()
    return mod.sns_client


def _daily_alert_published(sns_client) -> bool:
    """True if any SNS publish call carried a Daily Token Quota alert."""
    for call in sns_client.publish.call_args_list:
        subject = call.kwargs.get("Subject", "")
        if "Daily Token Quota" in subject:
            return True
    return False


class TestStaleDailyReset:
    def test_idle_user_stale_day_no_daily_alert(self, base_env):
        """daily_date=yesterday + daily_tokens over limit + no activity -> no alert."""
        mod = _load_quota_monitor(base_env)
        sns = _patch_monitor(
            mod,
            scan_item={
                "email": "idle.user@example.com",
                "total_tokens": 3_355_533,
                "daily_tokens": 3_355_533,  # frozen, above 2,000,000 limit
                "daily_date": _yesterday(),
            },
        )

        result = mod.lambda_handler({}, None)
        assert result["statusCode"] == 200
        assert not _daily_alert_published(sns), "stale daily counter must not re-alert"

    def test_active_user_same_day_over_limit_still_alerts(self, base_env):
        """daily_date=today + over limit -> daily alert IS generated (no over-correction)."""
        mod = _load_quota_monitor(base_env)
        sns = _patch_monitor(
            mod,
            scan_item={
                "email": "active.user@example.com",
                "total_tokens": 3_355_533,
                "daily_tokens": 3_355_533,  # above 2,000,000 limit, today
                "daily_date": _today(),
            },
        )

        result = mod.lambda_handler({}, None)
        assert result["statusCode"] == 200
        assert _daily_alert_published(sns), "genuine same-day over-limit must still alert"

    def test_build_usage_entry_zeros_stale_daily(self, base_env):
        """_build_usage_entry applies the stale-day guard; monthly is untouched."""
        mod = _load_quota_monitor(base_env)
        item = {
            "email": "idle.user@example.com",
            "total_tokens": 3_355_533,
            "daily_tokens": 3_355_533,
            "daily_date": _yesterday(),
        }
        entry = mod._build_usage_entry(item, _today())
        assert entry["daily_tokens"] == 0
        assert entry["total_tokens"] == 3_355_533

    def test_build_usage_entry_keeps_same_day_daily(self, base_env):
        mod = _load_quota_monitor(base_env)
        item = {
            "email": "active.user@example.com",
            "total_tokens": 3_355_533,
            "daily_tokens": 3_355_533,
            "daily_date": _today(),
        }
        entry = mod._build_usage_entry(item, _today())
        assert entry["daily_tokens"] == 3_355_533


class TestCostEstimateTokenTypes:
    """Regression: cost must price all four token types, including cacheCreation.

    The metric's `type` dimension is camelCase (input/output/cacheRead/
    cacheCreation) but the rate tables use snake_case keys (input/output/
    cache_read/cache_write). The old code only rewrote cacheRead->cache_read, so
    cacheCreation looked up a non-existent key and priced at $0 — dropping the
    cache-write share of the cost, which is usually the majority of tokens.
    """

    def _run_cost(self, mod, type_model_vector):
        """Drive fetch_usage_from_promql with a canned type+model breakdown.

        Only the (user.email, type, model) query returns data; the primary
        total, type-only, and CoWork queries return empty so we isolate the
        cost calculation. Returns the users dict.
        """

        def fake_query(query, time_param=None):
            if ", type, model)" in query and "claude_code.token.usage" in query:
                return type_model_vector
            return []

        mod._promql_query = fake_query
        return mod.fetch_usage_from_promql()

    def test_cache_creation_is_priced(self, base_env):
        """A cacheCreation-only breakdown must produce a non-zero cost."""
        mod = _load_quota_monitor(base_env)
        # opus cache_write rate = 6.25 / 1M tokens; 1,000,000 tokens -> $6.25
        vector = [
            {
                "metric": {"user.email": "a@b.com", "type": "cacheCreation", "model": "claude-opus-4-8"},
                "value": [0, "1000000"],
            }
        ]
        users = self._run_cost(mod, vector)
        assert users["a@b.com"]["cost_usd"] == pytest.approx(6.25)

    def test_all_four_types_priced(self, base_env):
        """input/output/cacheRead/cacheCreation each contribute to the cost."""
        mod = _load_quota_monitor(base_env)
        # opus rates per 1M: input 5.00, output 25.00, cache_read 0.50, cache_write 6.25
        vector = [
            {"metric": {"user.email": "a@b.com", "type": "input", "model": "claude-opus-4-8"}, "value": [0, "1000000"]},
            {
                "metric": {"user.email": "a@b.com", "type": "output", "model": "claude-opus-4-8"},
                "value": [0, "1000000"],
            },
            {
                "metric": {"user.email": "a@b.com", "type": "cacheRead", "model": "claude-opus-4-8"},
                "value": [0, "1000000"],
            },
            {
                "metric": {"user.email": "a@b.com", "type": "cacheCreation", "model": "claude-opus-4-8"},
                "value": [0, "1000000"],
            },
        ]
        users = self._run_cost(mod, vector)
        assert users["a@b.com"]["cost_usd"] == pytest.approx(5.00 + 25.00 + 0.50 + 6.25)


class TestPromQLAggregationFunction:
    """Regression: token.usage is a Counter exported with DELTA temporality.

    increase() assumes cumulative temporality and misreads the delta sawtooth's
    down-steps as counter resets, returning empty/understated results. That froze
    the DynamoDB row and surfaced as "Daily Tokens: 0" in `gip quota usage` while
    Athena/CloudWatch (which sum the deltas) stayed correct. Aggregation MUST use
    sum_over_time(). The existing tests mock fetch_usage_from_promql out entirely,
    so the query construction — where the bug lived — was never exercised.
    """

    def _capture_queries(self, mod, primary_total="531643"):
        """Monkeypatch _promql_query to record every query string it receives.

        Returns the list that accumulates the queries. Only the primary
        per-user total query gets a non-empty vector so the aggregation has
        something to fold in; the rest return empty so cost/CoWork paths no-op.
        """
        captured = []

        def fake_query(query, time_param=None):
            captured.append(query)
            is_primary = 'sum by ("user.email")' in query and ", type" not in query and ", model" not in query
            if is_primary and "claude_code.token.usage" in query:
                return [{"metric": {"user.email": "a@b.com"}, "value": [0, primary_total]}]
            return []

        mod._promql_query = fake_query
        return captured

    def test_claude_code_queries_use_sum_over_time_not_increase(self, base_env):
        mod = _load_quota_monitor(base_env)
        captured = self._capture_queries(mod)

        mod.fetch_usage_from_promql()

        cc_queries = [q for q in captured if "claude_code.token.usage" in q]
        assert cc_queries, "expected at least one claude_code.token.usage query"
        for q in cc_queries:
            assert "sum_over_time(" in q, f"delta metric must use sum_over_time: {q}"
            assert "increase(" not in q, f"increase() is wrong for a delta metric: {q}"

    def test_cowork_queries_use_sum_over_time_not_increase(self, base_env):
        mod = _load_quota_monitor(base_env)
        captured = self._capture_queries(mod)

        mod.fetch_usage_from_promql()

        cowork_queries = [q for q in captured if "ClaudeCoWork" in q]
        assert cowork_queries, "expected CoWork token.usage queries"
        for q in cowork_queries:
            assert "sum_over_time(" in q, f"CoWork delta metric must use sum_over_time: {q}"
            assert "increase(" not in q, f"increase() is wrong for a delta metric: {q}"

    def test_aggregated_total_flows_through(self, base_env):
        """A non-empty primary vector must be recorded (not skipped as delta<=0)."""
        mod = _load_quota_monitor(base_env)
        self._capture_queries(mod, primary_total="531643")

        users = mod.fetch_usage_from_promql()

        assert users.get("a@b.com", {}).get("total_tokens") == 531643


def _patch_cost_monitor(mod, scan_item: dict):
    """Wire the monitor like _patch_monitor but keep the REAL resolve_user_quota.

    The env-default policy path is exactly what cost mode exercises, so the
    cost tests must not mock it away. Returns the MagicMock SNS client.
    """
    mod.fetch_usage_from_promql = MagicMock(return_value={})
    mod.quota_table = MagicMock()
    mod.quota_table.scan.return_value = _scan_response([scan_item])
    mod.quota_table.query.return_value = {"Items": []}
    mod.sns_client = MagicMock()
    return mod.sns_client


def _subjects(sns_client) -> list[str]:
    return [call.kwargs.get("Subject", "") for call in sns_client.publish.call_args_list]


class TestCostBudgetAlerts:
    """Cost-mode SNS budget warnings (regression: cost mode sent NO warnings).

    quota_monitor compared tokens against token thresholds only; a user at 85%
    of their $ budget never got an "approaching budget" alert, and in cost mode
    (monthly_token_limit=0) any usage produced a bogus token "exceeded" alert.
    """

    @pytest.fixture
    def cost_env(self):
        return {
            "QUOTA_TABLE": "TestQuotaTable",
            "POLICIES_TABLE": "TestPoliciesTable",
            "SNS_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:test-alerts",
            "ENABLE_FINEGRAINED_QUOTAS": "false",
            "MONTHLY_TOKEN_LIMIT": "0",
            "MONTHLY_COST_LIMIT_USD": "100",
            "DAILY_COST_LIMIT_USD": "10",
        }

    def test_85_pct_of_monthly_budget_sends_single_warning(self, cost_env):
        """$85 of a $100 budget -> exactly one monthly_cost alert at the 80% tier."""
        mod = _load_quota_monitor(cost_env)
        sns = _patch_cost_monitor(
            mod,
            scan_item={
                "email": "spender@example.com",
                "total_tokens": Decimal("5000000"),
                "daily_tokens": Decimal("1000000"),
                "estimated_cost": Decimal("85.0"),
                "daily_cost_usd": Decimal("1.0"),
                "daily_date": _today(),
            },
        )

        result = mod.lambda_handler({}, None)
        assert result["statusCode"] == 200

        subjects = _subjects(sns)
        assert len(subjects) == 1, f"expected exactly one alert, got {subjects}"
        assert "Monthly Budget" in subjects[0]
        assert "WARNING" in subjects[0]
        assert "Token Quota" not in subjects[0], "cost mode must not emit token alerts"
        message = sns.publish.call_args_list[0].kwargs["Message"]
        assert "$85.00" in message and "$100.00" in message

    def test_over_daily_budget_sends_daily_cost_alert(self, cost_env):
        """daily_cost_usd above the daily budget -> daily_cost exceeded alert."""
        mod = _load_quota_monitor(cost_env)
        sns = _patch_cost_monitor(
            mod,
            scan_item={
                "email": "spender@example.com",
                "total_tokens": Decimal("5000000"),
                "daily_tokens": Decimal("1000000"),
                "estimated_cost": Decimal("50.0"),
                "daily_cost_usd": Decimal("12.0"),
                "daily_date": _today(),
            },
        )

        result = mod.lambda_handler({}, None)
        assert result["statusCode"] == 200

        subjects = _subjects(sns)
        assert len(subjects) == 1, f"expected exactly one alert, got {subjects}"
        assert "Daily Budget" in subjects[0]
        assert "EXCEEDED" in subjects[0]
        message = sns.publish.call_args_list[0].kwargs["Message"]
        assert "$12.00" in message and "$10.00" in message

    def test_stale_daily_cost_does_not_alert(self, cost_env):
        """daily_cost_usd frozen from a prior day must be reset to 0 (stale-day guard)."""
        mod = _load_quota_monitor(cost_env)
        sns = _patch_cost_monitor(
            mod,
            scan_item={
                "email": "idle.spender@example.com",
                "total_tokens": Decimal("5000000"),
                "daily_tokens": Decimal("1000000"),
                "estimated_cost": Decimal("50.0"),
                "daily_cost_usd": Decimal("12.0"),
                "daily_date": _yesterday(),
            },
        )

        result = mod.lambda_handler({}, None)
        assert result["statusCode"] == 200
        assert sns.publish.call_count == 0, "stale daily cost must not re-alert"

    def test_token_only_policy_unchanged_no_cost_alerts(self, base_env):
        """No cost limits configured -> no cost alerts even with cost data present."""
        mod = _load_quota_monitor(base_env)
        sns = _patch_cost_monitor(
            mod,
            scan_item={
                "email": "tokens.only@example.com",
                "total_tokens": Decimal("3000000"),
                "daily_tokens": Decimal("1000000"),
                "estimated_cost": Decimal("999.0"),
                "daily_cost_usd": Decimal("999.0"),
                "daily_date": _today(),
            },
        )

        result = mod.lambda_handler({}, None)
        assert result["statusCode"] == 200
        assert sns.publish.call_count == 0, f"unexpected alerts: {_subjects(sns)}"

    def test_90_pct_tier_is_critical(self, cost_env):
        """$91 of a $100 budget -> monthly_cost critical (direct function check)."""
        mod = _load_quota_monitor(cost_env)
        policy = mod.resolve_user_quota("spender@example.com", [], {})
        alerts = mod.check_limits_and_generate_alerts(
            email="spender@example.com",
            total_tokens=0,
            daily_tokens=0,
            policy=policy,
            month_name="July 2026",
            current_date=_today(),
            days_remaining=10,
            days_in_month=31,
            sent_alerts=set(),
            estimated_cost=91.0,
            daily_cost_usd=0,
        )
        assert [(a["alert_type"], a["alert_level"]) for a in alerts] == [("monthly_cost", "critical")]

    def test_sent_alert_dedupe_uses_cost_alert_type(self, cost_env):
        """An already-sent monthly_cost warning is not regenerated; token keys don't collide."""
        mod = _load_quota_monitor(cost_env)
        policy = mod.resolve_user_quota("spender@example.com", [], {})
        alerts = mod.check_limits_and_generate_alerts(
            email="spender@example.com",
            total_tokens=0,
            daily_tokens=0,
            policy=policy,
            month_name="July 2026",
            current_date=_today(),
            days_remaining=10,
            days_in_month=31,
            sent_alerts={"spender@example.com#monthly_cost#warning"},
            estimated_cost=85.0,
            daily_cost_usd=0,
        )
        assert alerts == []


class TestCostEnvPolicyResolution:
    """resolve_user_quota env-default path must carry cost limits (like quota_check)."""

    def test_cost_only_env_returns_policy_with_cost_fields(self):
        mod = _load_quota_monitor(
            {
                "QUOTA_TABLE": "TestQuotaTable",
                "POLICIES_TABLE": "TestPoliciesTable",
                "SNS_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:test-alerts",
                "ENABLE_FINEGRAINED_QUOTAS": "false",
                "MONTHLY_TOKEN_LIMIT": "0",
                "MONTHLY_COST_LIMIT_USD": "100",
                "DAILY_COST_LIMIT_USD": "10",
            }
        )
        policy = mod.resolve_user_quota("a@b.com", [], {})
        assert policy is not None, "cost-only env limits must still yield a policy"
        assert policy["monthly_token_limit"] == 0
        assert policy["monthly_cost_limit"] == 100.0
        assert policy["daily_cost_limit"] == 10.0

    def test_all_zero_env_returns_none(self):
        mod = _load_quota_monitor(
            {
                "QUOTA_TABLE": "TestQuotaTable",
                "POLICIES_TABLE": "TestPoliciesTable",
                "SNS_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:test-alerts",
                "ENABLE_FINEGRAINED_QUOTAS": "false",
                "MONTHLY_TOKEN_LIMIT": "0",
                "MONTHLY_COST_LIMIT_USD": "0",
                "DAILY_COST_LIMIT_USD": "0",
            }
        )
        assert mod.resolve_user_quota("a@b.com", [], {}) is None


class TestMeteringDriftReconciliation:
    """Server-side metering reconciliation: drift metric + SNS alert.

    quota_monitor compares month-to-date client totals against the server_*
    figures accrued by the metering_processor Lambda. drift =
    (client - server) / max(server, 1); a stopped sidecar drives drift to
    -100%. Metric per user (only when server_total > 0); alert only when
    |drift| > 25% AND server_total > 1M tokens (noise floor).
    """

    @pytest.fixture(autouse=True)
    def _cleanup_env(self):
        yield
        os.environ.pop("DRIFT_ALERT_PERCENT", None)
        os.environ.pop("DRIFT_MIN_SERVER_TOKENS", None)

    def _run(self, base_env, scan_item):
        mod = _load_quota_monitor(base_env)
        sns = _patch_monitor(mod, scan_item=scan_item)
        mod.cloudwatch_client = MagicMock()
        result = mod.lambda_handler({}, None)
        assert result["statusCode"] == 200
        return mod, sns

    def _drift_metrics(self, mod):
        metrics = []
        for call in mod.cloudwatch_client.put_metric_data.call_args_list:
            assert call.kwargs["Namespace"] == "GIP/Quota"
            metrics.extend(call.kwargs["MetricData"])
        return [m for m in metrics if m["MetricName"] == "MeteringDrift"]

    def _drift_alert_subjects(self, sns):
        return [s for s in _subjects(sns) if "Metering Drift" in s]

    def test_drift_metric_published_per_user(self, base_env):
        mod, _sns = self._run(
            base_env,
            scan_item={
                "email": "drifter@example.com",
                "total_tokens": Decimal("2000000"),
                "daily_tokens": Decimal("0"),
                "daily_date": _today(),
                "server_total_tokens": Decimal("10000000"),
            },
        )
        metrics = self._drift_metrics(mod)
        assert len(metrics) == 1
        assert metrics[0]["Dimensions"] == [{"Name": "user.email", "Value": "drifter@example.com"}]
        assert metrics[0]["Value"] == pytest.approx(-80.0)

    def test_large_drift_over_noise_floor_alerts(self, base_env):
        """|drift| > 25% and server_total > 1M -> one metering_drift SNS alert."""
        mod, sns = self._run(
            base_env,
            scan_item={
                "email": "drifter@example.com",
                "total_tokens": Decimal("2000000"),
                "daily_tokens": Decimal("0"),
                "daily_date": _today(),
                "server_total_tokens": Decimal("10000000"),
            },
        )
        subjects = self._drift_alert_subjects(sns)
        assert len(subjects) == 1, f"expected one drift alert, got {_subjects(sns)}"
        message = sns.publish.call_args_list[0].kwargs["Message"]
        assert "client 2,000,000" in message and "server 10,000,000" in message

    def test_no_server_attrs_is_noop(self, base_env):
        """Deployments without the metering stack must see zero behavior change."""
        mod, sns = self._run(
            base_env,
            scan_item={
                "email": "classic@example.com",
                "total_tokens": Decimal("2000000"),
                "daily_tokens": Decimal("0"),
                "daily_date": _today(),
            },
        )
        assert self._drift_metrics(mod) == []
        assert self._drift_alert_subjects(sns) == []

    def test_small_drift_publishes_metric_but_no_alert(self, base_env):
        mod, sns = self._run(
            base_env,
            scan_item={
                "email": "aligned@example.com",
                "total_tokens": Decimal("9000000"),
                "daily_tokens": Decimal("0"),
                "daily_date": _today(),
                "server_total_tokens": Decimal("10000000"),
            },
        )
        assert len(self._drift_metrics(mod)) == 1  # -10% drift still published
        assert self._drift_alert_subjects(sns) == []

    def test_noise_floor_suppresses_alert_for_small_users(self, base_env):
        """100% drift on a tiny server total (<= 1M tokens) must not alert."""
        mod, sns = self._run(
            base_env,
            scan_item={
                "email": "tiny@example.com",
                "total_tokens": Decimal("0"),
                "daily_tokens": Decimal("0"),
                "daily_date": _today(),
                "server_total_tokens": Decimal("500000"),
            },
        )
        assert len(self._drift_metrics(mod)) == 1
        assert self._drift_alert_subjects(sns) == []

    def test_reconcile_metering_direct(self, base_env):
        mod = _load_quota_monitor(base_env)
        mod.cloudwatch_client = MagicMock()
        alerts = mod.reconcile_metering(
            {
                "over@example.com": {"total_tokens": 0, "server_total_tokens": 2_000_000},
                "fine@example.com": {"total_tokens": 1_000_000, "server_total_tokens": 1_100_000},
                "no-server@example.com": {"total_tokens": 5_000_000, "server_total_tokens": 0},
            }
        )
        assert [(a["user"], a["alert_type"], a["alert_level"]) for a in alerts] == [
            ("over@example.com", "metering_drift", "warning")
        ]
        assert alerts[0]["percentage"] == pytest.approx(-100.0)

    def test_unattributed_bucket_gets_metric_but_no_alert(self, base_env):
        mod = _load_quota_monitor(base_env)
        mod.cloudwatch_client = MagicMock()
        alerts = mod.reconcile_metering(
            {"UNATTRIBUTED#SomeRole": {"total_tokens": 0, "server_total_tokens": 5_000_000}}
        )
        assert alerts == []
        published = mod.cloudwatch_client.put_metric_data.call_args.kwargs["MetricData"]
        assert published[0]["Dimensions"] == [{"Name": "user.email", "Value": "UNATTRIBUTED#SomeRole"}]

    def test_build_usage_entry_carries_server_total(self, base_env):
        mod = _load_quota_monitor(base_env)
        entry = mod._build_usage_entry(
            {
                "email": "a@b.com",
                "total_tokens": 10,
                "daily_tokens": 0,
                "daily_date": _today(),
                "server_total_tokens": Decimal("123"),
            },
            _today(),
        )
        assert entry["server_total_tokens"] == 123

    def test_drift_alert_dedupe(self, base_env):
        """An already-sent metering_drift warning is not re-sent (same dedupe as other alerts)."""
        mod = _load_quota_monitor(base_env)
        sns = _patch_monitor(
            mod,
            scan_item={
                "email": "drifter@example.com",
                "total_tokens": Decimal("2000000"),
                "daily_tokens": Decimal("0"),
                "daily_date": _today(),
                "server_total_tokens": Decimal("10000000"),
            },
        )
        mod.cloudwatch_client = MagicMock()
        mod.get_sent_alerts = MagicMock(return_value={"drifter@example.com#metering_drift#warning"})

        result = mod.lambda_handler({}, None)
        assert result["statusCode"] == 200
        assert self._drift_alert_subjects(sns) == []


class TestLoadPoliciesCostFields:
    """load_all_policies must surface monthly_cost_limit/daily_cost_limit from DDB."""

    def test_cost_fields_read_on_first_page_and_pagination(self, base_env):
        mod = _load_quota_monitor(base_env)
        mod.policies_table = MagicMock()
        page1 = {
            "Items": [
                {
                    "policy_type": "user",
                    "identifier": "a@b.com",
                    "monthly_token_limit": Decimal("1000000000"),
                    "monthly_cost_limit": Decimal("50"),
                    "daily_cost_limit": Decimal("10"),
                    "enforcement_mode": "alert",
                    "enabled": True,
                }
            ],
            "LastEvaluatedKey": {"pk": "POLICY#user#a@b.com"},
        }
        page2 = {
            "Items": [
                {
                    "policy_type": "user",
                    "identifier": "b@b.com",
                    "monthly_token_limit": Decimal("1000000000"),
                    "monthly_cost_limit": Decimal("75.5"),
                    "enforcement_mode": "alert",
                    "enabled": True,
                },
                {
                    "policy_type": "user",
                    "identifier": "c@b.com",
                    "monthly_token_limit": Decimal("1000000000"),
                    "enforcement_mode": "alert",
                    "enabled": True,
                },
            ]
        }
        mod.policies_table.scan.side_effect = [page1, page2]

        policies = mod.load_all_policies()

        assert policies["user:a@b.com"]["monthly_cost_limit"] == 50.0
        assert policies["user:a@b.com"]["daily_cost_limit"] == 10.0
        assert policies["user:b@b.com"]["monthly_cost_limit"] == 75.5
        assert policies["user:b@b.com"]["daily_cost_limit"] is None
        assert policies["user:c@b.com"]["monthly_cost_limit"] is None
        assert policies["user:c@b.com"]["daily_cost_limit"] is None
