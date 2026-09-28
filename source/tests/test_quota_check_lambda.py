# ABOUTME: Tests for the quota_check Lambda function's daily enforcement logic
# ABOUTME: Covers both env-var (ENABLE_FINEGRAINED_QUOTAS=false) and DynamoDB-backed paths

"""Tests for quota_check Lambda daily enforcement (block vs alert)."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

LAMBDA_PATH = (
    Path(__file__).resolve().parents[2]
    / "deployment"
    / "infrastructure"
    / "lambda-functions"
    / "quota_check"
    / "index.py"
)


def _load_quota_check(env: dict) -> object:
    """Load the quota_check Lambda module fresh with the given environment.

    The module reads env vars at import time, so we must reload it after
    setting environment variables.
    """
    # Apply env vars before module import
    for key, value in env.items():
        os.environ[key] = value

    # Force a fresh import each time so module-level env reads take effect
    module_name = f"quota_check_index_{id(env)}"
    spec = importlib.util.spec_from_file_location(module_name, LAMBDA_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _build_event(email: str = "user@example.com", groups: list[str] | None = None) -> dict:
    claims: dict = {"email": email}
    if groups is not None:
        claims["groups"] = groups
    return {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}


def _parse(response: dict) -> dict:
    return json.loads(response["body"])


@pytest.fixture
def base_env():
    """Minimal env vars common to all tests."""
    return {
        "QUOTA_TABLE": "TestQuotaTable",
        "POLICIES_TABLE": "TestPoliciesTable",
        "MISSING_EMAIL_ENFORCEMENT": "block",
        "ERROR_HANDLING_MODE": "fail_closed",
    }


# ---------------------------------------------------------------------------
# Env-var path: ENABLE_FINEGRAINED_QUOTAS=false
# ---------------------------------------------------------------------------


class TestDailyEnforcementEnvVarPath:
    """ENABLE_FINEGRAINED_QUOTAS=false -> policy comes from env vars."""

    def _make_module(self, base_env, daily_mode: str):
        env = {
            **base_env,
            "ENABLE_FINEGRAINED_QUOTAS": "false",
            "MONTHLY_TOKEN_LIMIT": "1000",
            "DAILY_TOKEN_LIMIT": "100",
            "MONTHLY_ENFORCEMENT_MODE": "block",
            "DAILY_ENFORCEMENT_MODE": daily_mode,
        }
        return _load_quota_check(env)

    def _patch_usage_and_unblock(self, mod, daily_tokens: int, monthly_tokens: int = 0):
        mod.quota_table = MagicMock()
        # First call = unblock status (no item), second call = monthly usage
        mod.quota_table.get_item.side_effect = [
            {},  # no unblock entry
            {
                "Item": {
                    "total_tokens": monthly_tokens,
                    "daily_tokens": daily_tokens,
                    "daily_date": mod.datetime.now(mod.timezone.utc).strftime("%Y-%m-%d"),
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_tokens": 0,
                }
            },
        ]

    def test_daily_block_mode_blocks_when_exceeded(self, base_env):
        mod = self._make_module(base_env, daily_mode="block")
        self._patch_usage_and_unblock(mod, daily_tokens=150)

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is False
        assert body["reason"] == "daily_exceeded"

    def test_daily_alert_mode_allows_when_exceeded(self, base_env):
        mod = self._make_module(base_env, daily_mode="alert")
        self._patch_usage_and_unblock(mod, daily_tokens=150)

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is True
        assert body["reason"] == "within_quota"

    def test_daily_block_mode_allows_under_limit(self, base_env):
        mod = self._make_module(base_env, daily_mode="block")
        self._patch_usage_and_unblock(mod, daily_tokens=50)

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is True


# ---------------------------------------------------------------------------
# DynamoDB path: ENABLE_FINEGRAINED_QUOTAS=true
# ---------------------------------------------------------------------------


class TestDailyEnforcementFineGrainedPath:
    """ENABLE_FINEGRAINED_QUOTAS=true -> policy comes from DynamoDB.

    These tests cover the bug where get_policy() did not include
    daily_enforcement_mode in its returned dict, causing daily block mode
    to be silently downgraded to alert.
    """

    def _make_module(self, base_env):
        env = {
            **base_env,
            "ENABLE_FINEGRAINED_QUOTAS": "true",
        }
        return _load_quota_check(env)

    def _setup_mocks(
        self,
        mod,
        policy_item: dict,
        daily_tokens: int,
        monthly_tokens: int = 0,
    ):
        # policies_table: user policy hit
        mod.policies_table = MagicMock()
        mod.policies_table.get_item.return_value = {"Item": policy_item}

        # quota_table: no unblock, then monthly usage row
        mod.quota_table = MagicMock()
        mod.quota_table.get_item.side_effect = [
            {},  # unblock lookup
            {
                "Item": {
                    "total_tokens": monthly_tokens,
                    "daily_tokens": daily_tokens,
                    "daily_date": mod.datetime.now(mod.timezone.utc).strftime("%Y-%m-%d"),
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_tokens": 0,
                }
            },
        ]

    def test_get_policy_returns_daily_enforcement_mode(self, base_env):
        """get_policy() must include daily_enforcement_mode from DynamoDB."""
        mod = self._make_module(base_env)
        mod.policies_table = MagicMock()
        mod.policies_table.get_item.return_value = {
            "Item": {
                "policy_type": "user",
                "identifier": "user@example.com",
                "monthly_token_limit": 1000,
                "daily_token_limit": 100,
                "warning_threshold_80": 800,
                "warning_threshold_90": 900,
                "enforcement_mode": "block",
                "daily_enforcement_mode": "block",
                "enabled": True,
            }
        }

        policy = mod.get_policy("user", "user@example.com")
        assert policy is not None
        assert policy["daily_enforcement_mode"] == "block"

    def test_get_policy_defaults_daily_enforcement_mode_to_alert(self, base_env):
        """When DynamoDB item omits the field, default to 'alert'."""
        mod = self._make_module(base_env)
        mod.policies_table = MagicMock()
        mod.policies_table.get_item.return_value = {
            "Item": {
                "policy_type": "user",
                "identifier": "user@example.com",
                "monthly_token_limit": 1000,
                "daily_token_limit": 100,
                "warning_threshold_80": 800,
                "warning_threshold_90": 900,
                "enforcement_mode": "block",
                "enabled": True,
                # daily_enforcement_mode intentionally omitted
            }
        }

        policy = mod.get_policy("user", "user@example.com")
        assert policy["daily_enforcement_mode"] == "alert"

    def test_finegrained_daily_block_mode_blocks_when_exceeded(self, base_env):
        """Regression: daily_enforcement_mode='block' from DynamoDB must block."""
        mod = self._make_module(base_env)
        self._setup_mocks(
            mod,
            policy_item={
                "policy_type": "user",
                "identifier": "user@example.com",
                "monthly_token_limit": 1000,
                "daily_token_limit": 100,
                "warning_threshold_80": 800,
                "warning_threshold_90": 900,
                "enforcement_mode": "block",
                "daily_enforcement_mode": "block",
                "enabled": True,
            },
            daily_tokens=150,
        )

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is False
        assert body["reason"] == "daily_exceeded"

    def test_finegrained_daily_alert_mode_allows_when_exceeded(self, base_env):
        mod = self._make_module(base_env)
        self._setup_mocks(
            mod,
            policy_item={
                "policy_type": "user",
                "identifier": "user@example.com",
                "monthly_token_limit": 1000,
                "daily_token_limit": 100,
                "warning_threshold_80": 800,
                "warning_threshold_90": 900,
                "enforcement_mode": "block",
                "daily_enforcement_mode": "alert",
                "enabled": True,
            },
            daily_tokens=150,
        )

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is True
        assert body["reason"] == "within_quota"

    def test_finegrained_missing_daily_mode_defaults_to_alert(self, base_env):
        """If the DynamoDB item omits daily_enforcement_mode, treat as 'alert'."""
        mod = self._make_module(base_env)
        self._setup_mocks(
            mod,
            policy_item={
                "policy_type": "user",
                "identifier": "user@example.com",
                "monthly_token_limit": 1000,
                "daily_token_limit": 100,
                "warning_threshold_80": 800,
                "warning_threshold_90": 900,
                "enforcement_mode": "block",
                "enabled": True,
                # daily_enforcement_mode missing
            },
            daily_tokens=150,
        )

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is True
        assert body["reason"] == "within_quota"


# ---------------------------------------------------------------------------
# Contract tests: response schema validation
# ---------------------------------------------------------------------------


# Required keys in every quota_check response body
RESPONSE_REQUIRED_KEYS = {"allowed"}

# Keys expected when a quota policy exists and user is within quota
NORMAL_RESPONSE_KEYS = {"allowed", "reason", "enforcement_mode", "usage", "policy", "unblock_status", "message"}

# Valid values for 'reason' field
VALID_REASONS = {
    "within_quota",
    "monthly_exceeded",
    "daily_exceeded",
    "no_policy",
    "no_email",
    "unblocked",
    "missing_email_claim",
    "check_failed",
}

# Valid values for 'enforcement_mode' field
VALID_ENFORCEMENT_MODES = {"alert", "block", None}


class TestResponseSchemaContract:
    """Contract tests ensuring quota_check Lambda responses conform to expected schema.

    The credential-process binary parses these responses. If the schema changes,
    credential-process breaks silently (users get blocked or allowed incorrectly).
    These tests ensure both sides agree on the contract.
    """

    def _make_module(self, base_env, **overrides):
        env = {
            **base_env,
            "ENABLE_FINEGRAINED_QUOTAS": "false",
            "MONTHLY_TOKEN_LIMIT": "1000",
            "DAILY_TOKEN_LIMIT": "100",
            "MONTHLY_ENFORCEMENT_MODE": "block",
            "DAILY_ENFORCEMENT_MODE": "block",
            **overrides,
        }
        return _load_quota_check(env)

    def _patch_usage(self, mod, daily_tokens: int = 0, monthly_tokens: int = 0):
        mod.quota_table = MagicMock()
        mod.quota_table.get_item.side_effect = [
            {},  # unblock
            {
                "Item": {
                    "total_tokens": monthly_tokens,
                    "daily_tokens": daily_tokens,
                    "daily_date": mod.datetime.now(mod.timezone.utc).strftime("%Y-%m-%d"),
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_tokens": 0,
                }
            },
        ]

    def test_response_is_valid_json_with_status_code(self, base_env):
        """Lambda returns dict with statusCode and JSON-parseable body."""
        mod = self._make_module(base_env)
        self._patch_usage(mod, daily_tokens=0)

        response = mod.lambda_handler(_build_event(), None)
        assert "statusCode" in response
        assert "body" in response
        assert isinstance(response["statusCode"], int)
        body = json.loads(response["body"])
        assert isinstance(body, dict)

    def test_allowed_response_has_required_keys(self, base_env):
        """When allowed=True, response includes all expected keys."""
        mod = self._make_module(base_env)
        self._patch_usage(mod, daily_tokens=0)

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is True
        for key in NORMAL_RESPONSE_KEYS:
            assert key in body, f"Missing key '{key}' in allowed response"

    def test_blocked_response_has_required_keys(self, base_env):
        """When allowed=False, response includes all expected keys."""
        mod = self._make_module(base_env)
        self._patch_usage(mod, daily_tokens=150)  # exceeds 100 limit

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is False
        for key in NORMAL_RESPONSE_KEYS:
            assert key in body, f"Missing key '{key}' in blocked response"

    def test_reason_field_is_valid_enum(self, base_env):
        """'reason' field uses a known value."""
        mod = self._make_module(base_env)
        self._patch_usage(mod, daily_tokens=0)

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["reason"] in VALID_REASONS, f"Unknown reason: {body['reason']}"

    def test_enforcement_mode_is_valid(self, base_env):
        """'enforcement_mode' is alert, block, or None."""
        mod = self._make_module(base_env)
        self._patch_usage(mod, daily_tokens=0)

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["enforcement_mode"] in VALID_ENFORCEMENT_MODES

    def test_usage_summary_structure(self, base_env):
        """'usage' field contains expected token count keys."""
        mod = self._make_module(base_env)
        self._patch_usage(mod, daily_tokens=50, monthly_tokens=200)

        body = _parse(mod.lambda_handler(_build_event(), None))
        usage = body["usage"]
        assert usage is not None
        assert "monthly_tokens" in usage
        assert "monthly_limit" in usage
        assert "monthly_percent" in usage
        assert "daily_tokens" in usage
        assert "daily_limit" in usage

    def test_policy_field_structure(self, base_env):
        """'policy' field contains type and identifier."""
        mod = self._make_module(base_env)
        self._patch_usage(mod, daily_tokens=0)

        body = _parse(mod.lambda_handler(_build_event(), None))
        policy = body["policy"]
        assert policy is not None
        assert "type" in policy
        assert "identifier" in policy

    def test_unblock_status_structure(self, base_env):
        """'unblock_status' field contains is_unblocked boolean."""
        mod = self._make_module(base_env)
        self._patch_usage(mod, daily_tokens=0)

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert "unblock_status" in body
        assert "is_unblocked" in body["unblock_status"]
        assert isinstance(body["unblock_status"]["is_unblocked"], bool)

    def test_message_field_is_string(self, base_env):
        """'message' field is always a human-readable string."""
        mod = self._make_module(base_env)
        self._patch_usage(mod, daily_tokens=0)

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert isinstance(body["message"], str)
        assert len(body["message"]) > 0

    def test_monthly_exceeded_sets_reason_correctly(self, base_env):
        """Monthly limit exceeded returns reason='monthly_exceeded'."""
        mod = self._make_module(base_env)
        self._patch_usage(mod, monthly_tokens=1500)  # exceeds 1000 limit

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is False
        assert body["reason"] == "monthly_exceeded"

    def test_no_policy_response(self, base_env):
        """When MONTHLY_TOKEN_LIMIT=0 (disabled), returns no_policy."""
        mod = self._make_module(base_env, MONTHLY_TOKEN_LIMIT="0", DAILY_TOKEN_LIMIT="0")  # nosec B106
        self._patch_usage(mod)
        # With env limits 0 the resolver falls through to DynamoDB policy
        # lookups; a successful lookup with no items means "no policy".
        # (Previously this test leaked through to a real boto3 call that
        # get_policy swallowed — the F1 fix makes that failure propagate.)
        mod.policies_table = MagicMock()
        mod.policies_table.get_item.return_value = {}

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is True
        assert body["reason"] == "no_policy"


class TestInputValidationContract:
    """Contract tests for input handling — ensures malformed requests don't crash."""

    def _make_module(self, base_env):
        env = {
            **base_env,
            "ENABLE_FINEGRAINED_QUOTAS": "false",
            "MONTHLY_TOKEN_LIMIT": "1000",
            "DAILY_TOKEN_LIMIT": "100",
            "MONTHLY_ENFORCEMENT_MODE": "block",
            "DAILY_ENFORCEMENT_MODE": "block",
        }
        return _load_quota_check(env)

    def test_missing_email_claim(self, base_env):
        """Request with no email in JWT claims returns structured response."""
        mod = self._make_module(base_env)
        event = {"requestContext": {"authorizer": {"jwt": {"claims": {}}}}}

        response = mod.lambda_handler(event, None)
        assert response["statusCode"] == 200
        body = _parse(response)
        assert "allowed" in body
        assert body.get("reason") in ("missing_email_claim", "missing_identity")

    def test_missing_authorizer_context(self, base_env):
        """Request with no authorizer context does not crash."""
        mod = self._make_module(base_env)
        event = {"requestContext": {}}

        response = mod.lambda_handler(event, None)
        assert response["statusCode"] == 200
        body = _parse(response)
        assert "allowed" in body

    def test_empty_event(self, base_env):
        """Completely empty event does not crash the Lambda."""
        mod = self._make_module(base_env)

        response = mod.lambda_handler({}, None)
        assert response["statusCode"] == 200
        body = _parse(response)
        assert "allowed" in body

    def test_missing_email_blocked_by_default(self, base_env):
        """Missing email defaults to blocked (fail-closed security)."""
        env = {**base_env, "MISSING_EMAIL_ENFORCEMENT": "block"}
        mod = _load_quota_check(
            {
                **env,
                "ENABLE_FINEGRAINED_QUOTAS": "false",
                "MONTHLY_TOKEN_LIMIT": "1000",
                "DAILY_TOKEN_LIMIT": "100",
                "MONTHLY_ENFORCEMENT_MODE": "block",
                "DAILY_ENFORCEMENT_MODE": "block",
            }
        )
        event = {"requestContext": {"authorizer": {"jwt": {"claims": {}}}}}

        body = _parse(mod.lambda_handler(event, None))
        assert body["allowed"] is False


class TestFailModeValidation:
    """Fail-mode environment values are finite enums with secure fallbacks."""

    @pytest.mark.parametrize(
        ("mode", "expected_allowed"),
        [("block", False), ("warn", True)],
    )
    def test_supported_missing_identity_modes(self, base_env, mode, expected_allowed):
        mod = _load_quota_check({**base_env, "MISSING_EMAIL_ENFORCEMENT": mode})

        body = _parse(mod.lambda_handler({}, None))

        assert body["allowed"] is expected_allowed
        assert body["reason"] == "missing_identity"

    def test_invalid_missing_identity_mode_logs_and_fails_closed(self, base_env, capsys):
        mod = _load_quota_check({**base_env, "MISSING_EMAIL_ENFORCEMENT": "blokc"})

        startup_output = capsys.readouterr().out
        body = _parse(mod.lambda_handler({}, None))

        assert "ERROR: Invalid MISSING_EMAIL_ENFORCEMENT" in startup_output
        assert mod.MISSING_EMAIL_ENFORCEMENT == "block"
        assert body["allowed"] is False

    @pytest.mark.parametrize(
        ("mode", "expected_allowed"),
        [("fail_closed", False), ("fail_open", True)],
    )
    def test_supported_error_modes(self, base_env, mode, expected_allowed):
        mod = _load_quota_check({**base_env, "ERROR_HANDLING_MODE": mode})
        mod.resolve_quota_for_user = MagicMock(side_effect=RuntimeError("private detail"))

        body = _parse(mod.lambda_handler(_build_event(), None))

        assert body["allowed"] is expected_allowed
        assert "private detail" not in body["message"]

    def test_invalid_error_mode_logs_and_fails_closed(self, base_env, capsys):
        mod = _load_quota_check({**base_env, "ERROR_HANDLING_MODE": "fail-closed"})
        startup_output = capsys.readouterr().out
        mod.resolve_quota_for_user = MagicMock(side_effect=RuntimeError("private detail"))

        body = _parse(mod.lambda_handler(_build_event(), None))

        assert "ERROR: Invalid ERROR_HANDLING_MODE" in startup_output
        assert mod.ERROR_HANDLING_MODE == "fail_closed"
        assert body["allowed"] is False
        assert "private detail" not in body["message"]


class TestIdcUsernameIdentity:
    """Tests for IAM Identity Center username-based identity resolution (#592 feedback)."""

    @pytest.fixture
    def base_env(self):
        return {
            "AWS_DEFAULT_REGION": "us-east-1",
            "QUOTA_TABLE_NAME": "test-table",
            "ENABLE_FINEGRAINED_QUOTAS": "false",
            "MONTHLY_TOKEN_LIMIT": "1000000",
            "DAILY_TOKEN_LIMIT": "100000",
            "MONTHLY_ENFORCEMENT_MODE": "warn",
            "DAILY_ENFORCEMENT_MODE": "warn",
            "MISSING_EMAIL_ENFORCEMENT": "warn",
        }

    def test_email_session_name_resolves(self, base_env):
        """Standard case: IDC username is an email address."""
        mod = _load_quota_check(base_env)
        event = {
            "requestContext": {
                "authorizer": {"jwt": {"claims": {}}},
                "identity": {
                    "caller": "arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_BedrockDeveloper_abc123/user@company.com"
                },
            }
        }
        body = _parse(mod.lambda_handler(event, None))
        # Should resolve identity and not return missing_identity
        assert body.get("reason") != "missing_identity"

    def test_non_email_idc_username_resolves(self, base_env):
        """IDC username without @ (e.g. 'akshaya.claude') should still resolve."""
        mod = _load_quota_check(base_env)
        event = {
            "requestContext": {
                "authorizer": {"jwt": {"claims": {}}},
                "identity": {
                    "caller": "arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_BedrockDeveloper_abc123/akshaya.claude"
                },
            }
        }
        body = _parse(mod.lambda_handler(event, None))
        # Should resolve identity (not missing_identity)
        assert body.get("reason") != "missing_identity"

    def test_non_sso_role_without_email_does_not_resolve(self, base_env):
        """Non-SSO role without @ should NOT be treated as identity."""
        env = {**base_env, "MISSING_EMAIL_ENFORCEMENT": "block"}
        mod = _load_quota_check(env)
        event = {
            "requestContext": {
                "authorizer": {"jwt": {"claims": {}}},
                "identity": {"caller": "arn:aws:sts::123456789012:assumed-role/CustomRole/session123"},
            }
        }
        body = _parse(mod.lambda_handler(event, None))
        # Non-SSO role without email should be blocked
        assert body.get("reason") == "missing_identity"
        assert body["allowed"] is False


class TestCostBasedEnforcement:
    """Tests that cost-based quota enforcement reads the correct DDB attributes.

    Regression tests for issue #746: monthly cost enforcement was broken because:
    1. get_user_usage() didn't include cost fields in its return dict
    2. get_policy() didn't include monthly_cost_limit/daily_cost_limit
    3. The lookup key was 'cost_usd' but DDB attribute is 'estimated_cost'
    """

    def _make_module(self, base_env):
        env = {
            **base_env,
            "ENABLE_FINEGRAINED_QUOTAS": "true",
        }
        return _load_quota_check(env)

    def _patch_tables(
        self,
        mod,
        estimated_cost: float,
        monthly_cost_limit: float,
        daily_cost_usd: float = 0,
        daily_cost_limit: float = 0,
        monthly_token_limit: int = 0,
        quota_mode: str | None = "cost",
        total_tokens: int = 100_000,
        monthly_enforcement_mode: str = "block",
        daily_enforcement_mode: str = "block",
    ):
        """Mock both quota_table and policies_table with correct call sequence."""
        from datetime import datetime, timezone

        current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        mod.quota_table = MagicMock()
        # Call 1: get_unblock_status -> no unblock
        # Call 2: get_user_usage -> usage with estimated_cost
        mod.quota_table.get_item.side_effect = [
            {},  # unblock check: no item
            {
                "Item": {
                    "total_tokens": total_tokens,
                    "daily_tokens": 1000,
                    "daily_date": current_date,
                    "input_tokens": 60000,
                    "output_tokens": 40000,
                    "cache_tokens": 0,
                    "estimated_cost": estimated_cost,
                    "daily_cost_usd": daily_cost_usd,
                }
            },
        ]

        mod.policies_table = MagicMock()
        mod.policies_table.get_item.return_value = {
            "Item": {
                "pk": "POLICY#default#default",
                "sk": "CURRENT",
                "policy_type": "default",
                "identifier": "default",
                "monthly_token_limit": monthly_token_limit,
                "monthly_cost_limit": monthly_cost_limit,
                "daily_cost_limit": daily_cost_limit,
                "enforcement_mode": monthly_enforcement_mode,
                "daily_enforcement_mode": daily_enforcement_mode,
                "enabled": True,
                **({"quota_mode": quota_mode} if quota_mode else {}),
            }
        }

    def test_monthly_cost_blocks_when_exceeded(self, base_env):
        """When estimated_cost exceeds monthly_cost_limit, access must be denied."""
        mod = self._make_module(base_env)
        self._patch_tables(mod, estimated_cost=95.0, monthly_cost_limit=90.0)

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is False, (
            "Monthly cost enforcement failed: estimated_cost ($95) > limit ($90) but access was allowed. "
            "Check that get_user_usage includes 'estimated_cost' and get_policy includes 'monthly_cost_limit'."
        )
        assert body["reason"] == "monthly_cost_exceeded"

    def test_monthly_cost_allows_when_within_limit(self, base_env):
        """When estimated_cost is below monthly_cost_limit, access is granted."""
        mod = self._make_module(base_env)
        self._patch_tables(mod, estimated_cost=45.0, monthly_cost_limit=90.0)

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is True

    def test_legacy_cost_policy_ignores_synthetic_token_cap(self, base_env):
        mod = self._make_module(base_env)
        self._patch_tables(
            mod,
            estimated_cost=45.0,
            monthly_cost_limit=90.0,
            monthly_token_limit=1_000_000_000,
            quota_mode=None,
            total_tokens=2_000_000_000,
        )

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is True
        assert body["usage"]["monthly_limit"] == 0

    def test_explicit_cost_policy_token_cap_still_blocks(self, base_env):
        mod = self._make_module(base_env)
        self._patch_tables(
            mod,
            estimated_cost=45.0,
            monthly_cost_limit=90.0,
            monthly_token_limit=1_000,
            quota_mode="cost",
            total_tokens=1_000,
        )

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is False
        assert body["reason"] == "monthly_exceeded"

    def test_daily_cost_blocks_when_exceeded(self, base_env):
        """When daily_cost_usd exceeds daily_cost_limit, access must be denied."""
        mod = self._make_module(base_env)
        self._patch_tables(
            mod, estimated_cost=10.0, monthly_cost_limit=90.0, daily_cost_usd=12.0, daily_cost_limit=10.0
        )

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is False
        assert body["reason"] == "daily_cost_exceeded"

    def test_daily_cost_block_is_not_bypassed_by_monthly_alert_mode(self, base_env):
        mod = self._make_module(base_env)
        self._patch_tables(
            mod,
            estimated_cost=10.0,
            monthly_cost_limit=90.0,
            daily_cost_usd=12.0,
            daily_cost_limit=10.0,
            monthly_enforcement_mode="alert",
            daily_enforcement_mode="block",
        )

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is False
        assert body["reason"] == "daily_cost_exceeded"


# ---------------------------------------------------------------------------
# Server-side metering: METERING_MODE=max enforces on max(client, server_*)
# ---------------------------------------------------------------------------


class TestMeteringModeMax:
    """METERING_MODE=max must enforce on max(client, server_*) figures.

    server_* attributes are accrued by the metering_processor Lambda from
    Bedrock invocation logs. In the default 'shadow' mode behavior is
    byte-identical to pre-metering (regression tests below); in 'max' mode a
    stopped sidecar (client counters frozen) no longer reduces enforced usage.
    """

    @pytest.fixture(autouse=True)
    def _cleanup_env(self):
        """os.environ persists across module loads in this file — never leak METERING_MODE."""
        yield
        os.environ.pop("METERING_MODE", None)

    def _make_module(self, base_env, metering_mode: str):
        env = {
            **base_env,
            "ENABLE_FINEGRAINED_QUOTAS": "false",
            "MONTHLY_TOKEN_LIMIT": "400",
            "DAILY_TOKEN_LIMIT": "100",
            "MONTHLY_ENFORCEMENT_MODE": "block",
            "DAILY_ENFORCEMENT_MODE": "block",
            "METERING_MODE": metering_mode,
        }
        return _load_quota_check(env)

    def _patch_item(self, mod, item: dict):
        mod.quota_table = MagicMock()
        mod.quota_table.get_item.side_effect = [{}, {"Item": item}]

    def _item(self, mod, **overrides):
        today = mod.datetime.now(mod.timezone.utc).strftime("%Y-%m-%d")
        item = {
            "total_tokens": 100,
            "daily_tokens": 10,
            "daily_date": today,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_tokens": 0,
            "estimated_cost": 1.0,
            "daily_cost_usd": 0.5,
        }
        item.update(overrides)
        return item

    def test_max_mode_blocks_on_server_monthly_total(self, base_env):
        """Client 100 / server 500 with a 400 limit -> max mode must block."""
        mod = self._make_module(base_env, "max")
        self._patch_item(mod, self._item(mod, server_total_tokens=500))

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is False
        assert body["reason"] == "monthly_exceeded"

    def test_shadow_mode_ignores_server_total(self, base_env):
        """Default shadow mode: same data must keep today's client-only behavior."""
        mod = self._make_module(base_env, "shadow")
        self._patch_item(mod, self._item(mod, server_total_tokens=500))

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is True

    def test_max_mode_blocks_on_server_daily_total(self, base_env):
        mod = self._make_module(base_env, "max")
        today = mod.datetime.now(mod.timezone.utc).strftime("%Y-%m-%d")
        self._patch_item(
            mod,
            self._item(mod, server_total_tokens=200, server_daily_tokens=150, server_daily_date=today),
        )

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is False
        assert body["reason"] == "daily_exceeded"

    def test_max_mode_applies_stale_day_guard_to_server_daily(self, base_env):
        """A frozen server_daily_tokens from a prior day must read as 0 (same guard as client)."""
        mod = self._make_module(base_env, "max")
        yesterday = (mod.datetime.now(mod.timezone.utc) - __import__("datetime").timedelta(days=1)).strftime("%Y-%m-%d")
        self._patch_item(
            mod,
            self._item(mod, server_total_tokens=200, server_daily_tokens=150, server_daily_date=yesterday),
        )

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is True

    def test_max_mode_uses_server_cost_fields(self, base_env):
        mod = self._make_module(base_env, "max")
        today = mod.datetime.now(mod.timezone.utc).strftime("%Y-%m-%d")
        mod.quota_table = MagicMock()
        mod.quota_table.get_item.return_value = {
            "Item": self._item(
                mod,
                server_total_tokens=50,
                server_estimated_cost=42.0,
                server_daily_cost_usd=7.5,
                server_daily_date=today,
            )
        }

        usage = mod.get_user_usage("user@example.com")
        assert usage["total_tokens"] == 100  # client still higher
        assert usage["estimated_cost"] == 42.0  # server higher
        assert usage["daily_cost_usd"] == 7.5

    def test_max_mode_without_server_attrs_matches_client(self, base_env):
        """Regression: absent server_* attrs read as 0, so max() degenerates to client figures."""
        mod = self._make_module(base_env, "max")
        mod.quota_table = MagicMock()
        item = self._item(mod)
        mod.quota_table.get_item.return_value = {"Item": item}

        usage = mod.get_user_usage("user@example.com")
        assert usage["total_tokens"] == 100
        assert usage["daily_tokens"] == 10
        assert usage["estimated_cost"] == 1.0
        assert usage["daily_cost_usd"] == 0.5


# ---------------------------------------------------------------------------
# Fail-closed enforcement: DynamoDB failures must not read as "unlimited"
# ---------------------------------------------------------------------------


class TestInfrastructureFailureEnforcement:
    """Regression tests for review F1/F2: swallowed DynamoDB errors defeated fail-closed.

    get_policy() used to catch all exceptions and return None ("no policy =
    unlimited"); get_user_usage() returned all-zeros. A throttled table turned
    every blocked user into an unlimited user while ERROR_HANDLING_MODE=
    fail_closed sat inert. Infrastructure failures must now propagate to the
    top-level handler, which honors ERROR_HANDLING_MODE (deny in fail_closed,
    allow with a visible warning in fail_open). Genuinely-empty lookups (no
    policy item, no usage item) keep their "no item" semantics.
    """

    @pytest.fixture(autouse=True)
    def _cleanup_env(self):
        """os.environ persists across module loads — never leak fail_open."""
        yield
        os.environ.pop("ERROR_HANDLING_MODE", None)

    @staticmethod
    def _throttle() -> ClientError:
        return ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "rate exceeded"}},
            "GetItem",
        )

    def _make_finegrained_module(self, base_env, error_mode: str):
        env = {
            **base_env,
            "ENABLE_FINEGRAINED_QUOTAS": "true",
            "ERROR_HANDLING_MODE": error_mode,
        }
        return _load_quota_check(env)

    def _make_env_module(self, base_env, error_mode: str):
        env = {
            **base_env,
            "ENABLE_FINEGRAINED_QUOTAS": "false",
            "MONTHLY_TOKEN_LIMIT": "1000",
            "DAILY_TOKEN_LIMIT": "100",
            "MONTHLY_ENFORCEMENT_MODE": "block",
            "DAILY_ENFORCEMENT_MODE": "block",
            "ERROR_HANDLING_MODE": error_mode,
        }
        return _load_quota_check(env)

    # -- F1: policy lookup fails -------------------------------------------

    def test_policy_lookup_throttle_denies_in_fail_closed(self, base_env):
        """DDB throttling on the policies table must deny, not grant unlimited."""
        mod = self._make_finegrained_module(base_env, "fail_closed")
        mod.policies_table = MagicMock()
        mod.policies_table.get_item.side_effect = self._throttle()

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is False
        assert body["reason"] == "check_failed"

    def test_policy_lookup_throttle_allows_with_warning_in_fail_open(self, base_env):
        """fail_open still allows — but the failure must be visible in the response."""
        mod = self._make_finegrained_module(base_env, "fail_open")
        mod.policies_table = MagicMock()
        mod.policies_table.get_item.side_effect = self._throttle()

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is True
        assert body["reason"] == "check_failed"
        assert "Quota check failed" in body["message"]
        assert "fail_open" in body["message"]

    def test_get_policy_propagates_infrastructure_errors(self, base_env):
        mod = self._make_finegrained_module(base_env, "fail_closed")
        mod.policies_table = MagicMock()
        mod.policies_table.get_item.side_effect = self._throttle()

        with pytest.raises(ClientError):
            mod.get_policy("user", "user@example.com")

    def test_get_policy_returns_none_when_item_absent(self, base_env):
        """'No item' is not an error: a successful empty lookup still means no policy."""
        mod = self._make_finegrained_module(base_env, "fail_closed")
        mod.policies_table = MagicMock()
        mod.policies_table.get_item.return_value = {}

        assert mod.get_policy("user", "user@example.com") is None

    def test_no_policy_still_allows_in_fail_closed(self, base_env):
        """All policy lookups succeed with no items -> no_policy -> unlimited (unchanged)."""
        mod = self._make_finegrained_module(base_env, "fail_closed")
        mod.policies_table = MagicMock()
        mod.policies_table.get_item.return_value = {}

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is True
        assert body["reason"] == "no_policy"

    # -- F2: usage read fails ----------------------------------------------

    def test_usage_read_throttle_denies_in_fail_closed(self, base_env):
        """An over-quota user must not be read as zero usage during throttling."""
        mod = self._make_env_module(base_env, "fail_closed")
        mod.quota_table = MagicMock()
        # Unblock lookup succeeds (no item); the usage read throttles.
        mod.quota_table.get_item.side_effect = [{}, self._throttle()]

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is False
        assert body["reason"] == "check_failed"

    def test_usage_read_throttle_allows_with_warning_in_fail_open(self, base_env):
        mod = self._make_env_module(base_env, "fail_open")
        mod.quota_table = MagicMock()
        mod.quota_table.get_item.side_effect = [{}, self._throttle()]

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is True
        assert body["reason"] == "check_failed"
        assert "Quota check failed" in body["message"]

    def test_get_user_usage_propagates_infrastructure_errors(self, base_env):
        mod = self._make_env_module(base_env, "fail_closed")
        mod.quota_table = MagicMock()
        mod.quota_table.get_item.side_effect = self._throttle()

        with pytest.raises(ClientError):
            mod.get_user_usage("user@example.com")

    def test_get_user_usage_returns_zeros_when_item_absent(self, base_env):
        """'No month item yet' is not an error: zeros are correct for a fresh user."""
        mod = self._make_env_module(base_env, "fail_closed")
        mod.quota_table = MagicMock()
        mod.quota_table.get_item.return_value = {}

        usage = mod.get_user_usage("user@example.com")
        assert usage["total_tokens"] == 0
        assert usage["estimated_cost"] == 0

    def test_default_error_mode_is_fail_closed(self, base_env):
        """Without ERROR_HANDLING_MODE set, failures must deny (A5 fail-closed default)."""
        env = {k: v for k, v in base_env.items() if k != "ERROR_HANDLING_MODE"}
        os.environ.pop("ERROR_HANDLING_MODE", None)
        mod = _load_quota_check(
            {
                **env,
                "ENABLE_FINEGRAINED_QUOTAS": "false",
                "MONTHLY_TOKEN_LIMIT": "1000",
                "DAILY_TOKEN_LIMIT": "100",
                "MONTHLY_ENFORCEMENT_MODE": "block",
                "DAILY_ENFORCEMENT_MODE": "block",
            }
        )
        mod.quota_table = MagicMock()
        mod.quota_table.get_item.side_effect = [{}, self._throttle()]

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is False

    # -- unblock lookup keeps its deliberate fail-closed swallow ------------

    def test_unblock_lookup_failure_denies_override_only(self, base_env):
        """get_unblock_status failure means 'no override' — enforcement continues."""
        mod = self._make_env_module(base_env, "fail_closed")
        mod.quota_table = MagicMock()
        # Unblock lookup throttles (swallowed -> no override); usage read succeeds.
        mod.quota_table.get_item.side_effect = [
            self._throttle(),
            {
                "Item": {
                    "total_tokens": 0,
                    "daily_tokens": 0,
                    "daily_date": mod.datetime.now(mod.timezone.utc).strftime("%Y-%m-%d"),
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_tokens": 0,
                }
            },
        ]

        body = _parse(mod.lambda_handler(_build_event(), None))
        assert body["allowed"] is True
        assert body["reason"] == "within_quota"
