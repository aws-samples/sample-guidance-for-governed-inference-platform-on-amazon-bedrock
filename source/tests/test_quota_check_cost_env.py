# ABOUTME: Regression tests for cost-mode quota enforcement via environment defaults
# ABOUTME: Covers the env-var path (ENABLE_FINEGRAINED_QUOTAS=false) with USD cost limits

"""Regression tests: cost-mode env defaults must enforce without DynamoDB policies.

Bug: when an admin selected cost-based quotas in `gip init` (the recommended
default), the wizard zeroed MONTHLY_TOKEN_LIMIT and the quota_check Lambda's
env-default path required MONTHLY_TOKEN_LIMIT > 0, so `resolve_quota_for_user`
returned None (= unlimited) and cost limits were never enforced unless the
admin separately created fine-grained DynamoDB policies. The env-default
policy dict also dropped the cost fields entirely.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

LAMBDA_PATH = (
    Path(__file__).resolve().parents[2]
    / "deployment"
    / "infrastructure"
    / "lambda-functions"
    / "quota_check"
    / "index.py"
)


def _load_quota_check(env: dict) -> object:
    """Load the quota_check Lambda module fresh with the given environment."""
    for key, value in env.items():
        os.environ[key] = value

    module_name = f"quota_check_cost_index_{id(env)}"
    spec = importlib.util.spec_from_file_location(module_name, LAMBDA_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _build_event(email: str = "user@example.com") -> dict:
    return {"requestContext": {"authorizer": {"jwt": {"claims": {"email": email}}}}}


def _parse(response: dict) -> dict:
    return json.loads(response["body"])


@pytest.fixture
def cost_env():
    """Env for a cost-mode deployment: token limits zeroed, USD budgets set."""
    return {
        "QUOTA_TABLE": "TestQuotaTable",
        "POLICIES_TABLE": "TestPoliciesTable",
        "MISSING_EMAIL_ENFORCEMENT": "block",
        "ERROR_HANDLING_MODE": "fail_closed",
        "ENABLE_FINEGRAINED_QUOTAS": "false",
        "QUOTA_MODE": "cost",
        "MONTHLY_TOKEN_LIMIT": "0",
        "DAILY_TOKEN_LIMIT": "0",
        "MONTHLY_COST_LIMIT_USD": "50",
        "DAILY_COST_LIMIT_USD": "10",
        "MONTHLY_ENFORCEMENT_MODE": "block",
        "DAILY_ENFORCEMENT_MODE": "block",
    }


def _patch_usage_and_unblock(mod, estimated_cost: float, daily_cost: float = 0, total_tokens: int = 0):
    mod.quota_table = MagicMock()
    mod.quota_table.get_item.side_effect = [
        {},  # no unblock entry
        {
            "Item": {
                "total_tokens": total_tokens,
                "daily_tokens": 0,
                "daily_date": mod.datetime.now(mod.timezone.utc).strftime("%Y-%m-%d"),
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_tokens": 0,
                "estimated_cost": estimated_cost,
                "daily_cost_usd": daily_cost,
            }
        },
    ]


class TestCostModeEnvVarPath:
    """ENABLE_FINEGRAINED_QUOTAS=false + cost limits from environment."""

    def test_env_default_policy_carries_cost_limits(self, cost_env):
        """resolve_quota_for_user must return a policy when only cost limits are set."""
        mod = _load_quota_check(cost_env)
        policy = mod.resolve_quota_for_user("user@example.com", [])
        assert policy is not None, "cost-only env defaults must not resolve to unlimited"
        assert policy["monthly_cost_limit"] == 50.0
        assert policy["daily_cost_limit"] == 10.0

    def test_monthly_cost_exceeded_blocks(self, cost_env):
        mod = _load_quota_check(cost_env)
        _patch_usage_and_unblock(mod, estimated_cost=75.0)

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is False
        assert body["reason"] == "monthly_cost_exceeded"

    def test_daily_cost_exceeded_blocks(self, cost_env):
        mod = _load_quota_check(cost_env)
        _patch_usage_and_unblock(mod, estimated_cost=5.0, daily_cost=12.0)

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is False
        assert body["reason"] == "daily_cost_exceeded"

    def test_under_cost_limits_allows(self, cost_env):
        mod = _load_quota_check(cost_env)
        _patch_usage_and_unblock(mod, estimated_cost=25.0, daily_cost=5.0)

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is True
        assert body["reason"] == "within_quota"

    def test_high_token_usage_under_cost_limit_allows(self, cost_env):
        mod = _load_quota_check(cost_env)
        _patch_usage_and_unblock(mod, estimated_cost=25.0, total_tokens=2_000_000_000)

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is True
        assert body["reason"] == "within_quota"

    def test_cost_at_limit_blocks(self, cost_env):
        mod = _load_quota_check(cost_env)
        _patch_usage_and_unblock(mod, estimated_cost=50.0)

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is False
        assert body["reason"] == "monthly_cost_exceeded"

    def test_token_mode_unaffected_when_cost_limits_zero(self, cost_env):
        """Token-mode deployments (cost limits 0) must behave exactly as before."""
        env = {
            **cost_env,
            "MONTHLY_COST_LIMIT_USD": "0",
            "DAILY_COST_LIMIT_USD": "0",
            "MONTHLY_TOKEN_LIMIT": "1000",
        }
        mod = _load_quota_check(env)
        _patch_usage_and_unblock(mod, estimated_cost=999999.0)

        # estimated_cost is huge but cost limits are disabled; token usage 0 < 1000
        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is True

    def test_all_limits_zero_resolves_unlimited(self, cost_env):
        env = {
            **cost_env,
            "MONTHLY_COST_LIMIT_USD": "0",
            "DAILY_COST_LIMIT_USD": "0",
            "MONTHLY_TOKEN_LIMIT": "0",
        }
        mod = _load_quota_check(env)
        # All limits zero -> the resolver falls through to DynamoDB policy
        # lookups; successful lookups with no items mean unlimited. (The old
        # get_policy swallowed the real boto3 call this test used to leak.)
        mod.policies_table = MagicMock()
        mod.policies_table.get_item.return_value = {}
        assert mod.resolve_quota_for_user("user@example.com", []) is None
