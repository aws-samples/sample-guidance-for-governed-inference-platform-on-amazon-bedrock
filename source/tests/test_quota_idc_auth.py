# ABOUTME: Tests for dual-auth quota check (JWT + IAM Identity Center)
# ABOUTME: Verifies identity resolution from both OIDC JWT claims and IAM caller ARN

"""Tests for quota_check Lambda dual identity resolution.

These tests verify the identity extraction logic ONLY — they don't test
the full quota calculation flow (that's tested in test_quota_check_lambda.py).
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

# Add the lambda function to path
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__), "..", "..", "deployment", "infrastructure", "lambda-functions", "quota_check"
    ),
)

# Lambda initializes boto3 clients at module level; set region to avoid NoRegionError
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

import index


class TestIdentityResolution:
    """Test that the Lambda correctly extracts user identity from JWT or IAM ARN."""

    def _make_event(self, jwt_claims=None, caller_arn=None, iam_user_arn=None):
        """Build a mock API Gateway event.

        caller_arn   → legacy payload format 1.0 shape (requestContext.identity)
        iam_user_arn → HTTP API payload format 2.0 shape
                       (requestContext.authorizer.iam.userArn)
        """
        event = {"requestContext": {}}
        if jwt_claims:
            event["requestContext"]["authorizer"] = {"jwt": {"claims": jwt_claims}}
        if iam_user_arn:
            authorizer = event["requestContext"].setdefault("authorizer", {})
            authorizer["iam"] = {
                "userArn": iam_user_arn,
                "callerId": "AROAEXAMPLEID:session",
                "accountId": "123456789012",
            }
        if caller_arn:
            event["requestContext"]["identity"] = {"caller": caller_arn, "userArn": caller_arn}
        return event

    def _unlimited_policy(self):
        return {
            "monthly_limit": 225000000,
            "daily_limit": 0,
            "enforcement_mode": "alert",
            "enabled": True,
            "warning_threshold_80": 180000000,
            "warning_threshold_90": 202500000,
        }

    def test_idc_user_with_v2_iam_authorizer_context(self):
        """IDC user on an HTTP API v2 route: identity comes from authorizer.iam.userArn.

        Regression test for the quota route being deployed with
        PayloadFormatVersion '2.0' and AuthorizationType AWS_IAM
        (quota-monitoring.yaml). A v2 IAM event carries no
        requestContext.identity, so reading only the legacy location resolved
        no identity and the request failed closed with missing_identity.
        """
        event = self._make_event(
            iam_user_arn="arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_BedrockAccess_abc123/dave@company.com"
        )

        with (
            patch.object(index, "resolve_quota_for_user") as mock_resolve,
            patch.object(index, "get_unblock_status", return_value=None),
            patch.object(index, "get_user_usage_summary", return_value={}),
            patch.object(index, "get_user_usage", return_value={}),
        ):
            mock_resolve.return_value = self._unlimited_policy()

            result = index.lambda_handler(event, None)
            body = json.loads(result["body"])

            mock_resolve.assert_called_once_with("dave@company.com", [])
            assert body["allowed"] is True
            assert body.get("reason") != "missing_identity"

    def test_v2_iam_username_without_email(self):
        """IDC username without @ on a v2 event still resolves via AWSReservedSSO."""
        event = self._make_event(
            iam_user_arn="arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_BedrockAccess_abc123/erin.claude"
        )

        with (
            patch.object(index, "resolve_quota_for_user") as mock_resolve,
            patch.object(index, "get_unblock_status", return_value=None),
            patch.object(index, "get_user_usage_summary", return_value={}),
            patch.object(index, "get_user_usage", return_value={}),
        ):
            mock_resolve.return_value = self._unlimited_policy()

            result = index.lambda_handler(event, None)

            mock_resolve.assert_called_once_with("erin.claude", [])
            assert json.loads(result["body"])["allowed"] is True

    def test_v2_iam_context_preferred_over_legacy_identity(self):
        """When both shapes are present, the v2 authorizer.iam ARN wins."""
        event = self._make_event(
            caller_arn="arn:aws:sts::123456789012:assumed-role/Role/legacy@company.com",
            iam_user_arn="arn:aws:sts::123456789012:assumed-role/Role/v2user@company.com",
        )

        with (
            patch.object(index, "resolve_quota_for_user") as mock_resolve,
            patch.object(index, "get_unblock_status", return_value=None),
            patch.object(index, "get_user_usage_summary", return_value={}),
            patch.object(index, "get_user_usage", return_value={}),
        ):
            mock_resolve.return_value = self._unlimited_policy()

            index.lambda_handler(event, None)

            mock_resolve.assert_called_once_with("v2user@company.com", [])

    def test_jwt_preferred_over_v2_iam_context(self):
        """JWT claims still take priority over the v2 IAM ARN."""
        event = self._make_event(
            jwt_claims={"email": "jwt-user@company.com", "sub": "abc"},
            iam_user_arn="arn:aws:sts::123456789012:assumed-role/Role/iam-user@company.com",
        )

        with (
            patch.object(index, "resolve_quota_for_user") as mock_resolve,
            patch.object(index, "get_unblock_status", return_value=None),
            patch.object(index, "get_user_usage_summary", return_value={}),
            patch.object(index, "get_user_usage", return_value={}),
        ):
            mock_resolve.return_value = self._unlimited_policy()

            index.lambda_handler(event, None)

            mock_resolve.assert_called_once_with("jwt-user@company.com", [])

    def test_oidc_user_with_email_claim(self):
        """OIDC user: identity resolved from JWT email claim."""
        event = self._make_event(jwt_claims={"email": "alice@company.com", "sub": "abc123"})

        with (
            patch.object(index, "resolve_quota_for_user") as mock_resolve,
            patch.object(index, "get_unblock_status", return_value=None),
            patch.object(index, "get_user_usage_summary", return_value={}),
            patch.object(index, "get_user_usage", return_value={}),
        ):
            mock_resolve.return_value = {
                "monthly_limit": 225000000,
                "daily_limit": 0,
                "enforcement_mode": "alert",
                "enabled": True,
                "warning_threshold_80": 180000000,
                "warning_threshold_90": 202500000,
            }

            result = index.lambda_handler(event, None)
            body = json.loads(result["body"])

            mock_resolve.assert_called_once_with("alice@company.com", [])
            assert body["allowed"] is True

    def test_idc_user_with_email_in_arn(self):
        """IDC user: identity resolved from assumed-role ARN session name."""
        event = self._make_event(
            caller_arn="arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_BedrockAccess_abc123/bob@company.com"
        )

        with (
            patch.object(index, "resolve_quota_for_user") as mock_resolve,
            patch.object(index, "get_unblock_status", return_value=None),
            patch.object(index, "get_user_usage_summary", return_value={}),
            patch.object(index, "get_user_usage", return_value={}),
        ):
            mock_resolve.return_value = {
                "monthly_limit": 225000000,
                "daily_limit": 0,
                "enforcement_mode": "alert",
                "enabled": True,
                "warning_threshold_80": 180000000,
                "warning_threshold_90": 202500000,
            }

            result = index.lambda_handler(event, None)
            body = json.loads(result["body"])

            mock_resolve.assert_called_once_with("bob@company.com", [])
            assert body["allowed"] is True

    def test_idc_user_arn_without_email(self):
        """IDC user with ARN that has no email in session name — resolves as username identity.

        Per #597, usernames without @ are now valid identity (not rejected as missing).
        The Lambda resolves the session name and proceeds to quota check.
        """
        event = self._make_event(
            caller_arn="arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_BedrockAccess_abc123/session123"
        )

        # Successful-but-empty policy lookups = "no policy". (The old get_policy
        # swallowed the real boto3 call this test used to leak; infrastructure
        # errors now propagate, so the lookup must be mocked.)
        policies_table = MagicMock()
        policies_table.get_item.return_value = {}
        with patch.object(index, "policies_table", policies_table):
            result = index.lambda_handler(event, None)
        body = json.loads(result["body"])

        # Identity is resolved (session123), but no quota policy exists for this user
        assert body["allowed"] is True
        assert body.get("reason") in ("no_policy", None) or "identity" not in body.get("reason", "")

    def test_no_auth_at_all(self):
        """No JWT claims and no IAM identity — missing identity."""
        event = self._make_event()

        result = index.lambda_handler(event, None)
        body = json.loads(result["body"])

        assert body["reason"] == "missing_identity"

    def test_jwt_without_email_falls_through_to_arn(self):
        """JWT present but missing email claim — falls through to IAM ARN."""
        event = self._make_event(
            jwt_claims={"sub": "abc123"},  # no email
            caller_arn="arn:aws:sts::123456789012:assumed-role/Role/carol@company.com",
        )

        with (
            patch.object(index, "resolve_quota_for_user") as mock_resolve,
            patch.object(index, "get_unblock_status", return_value=None),
            patch.object(index, "get_user_usage_summary", return_value={}),
            patch.object(index, "get_user_usage", return_value={}),
        ):
            mock_resolve.return_value = {
                "monthly_limit": 225000000,
                "daily_limit": 0,
                "enforcement_mode": "alert",
                "enabled": True,
                "warning_threshold_80": 180000000,
                "warning_threshold_90": 202500000,
            }

            result = index.lambda_handler(event, None)
            body = json.loads(result["body"])

            mock_resolve.assert_called_once_with("carol@company.com", [])
            assert body["allowed"] is True

    def test_malformed_arn_no_slash(self):
        """Malformed ARN without slashes — should not crash."""
        event = self._make_event(caller_arn="not-a-valid-arn")

        result = index.lambda_handler(event, None)
        body = json.loads(result["body"])

        # "not-a-valid-arn" split by "/" gives ["not-a-valid-arn"] — no @ → missing identity
        assert body["reason"] == "missing_identity"

    def test_jwt_preferred_over_arn_when_both_present(self):
        """When both JWT email and ARN are available, JWT takes priority."""
        event = self._make_event(
            jwt_claims={"email": "jwt-user@company.com", "sub": "abc"},
            caller_arn="arn:aws:sts::123456789012:assumed-role/Role/arn-user@company.com",
        )

        with (
            patch.object(index, "resolve_quota_for_user") as mock_resolve,
            patch.object(index, "get_unblock_status", return_value=None),
            patch.object(index, "get_user_usage_summary", return_value={}),
            patch.object(index, "get_user_usage", return_value={}),
        ):
            mock_resolve.return_value = {
                "monthly_limit": 225000000,
                "daily_limit": 0,
                "enforcement_mode": "alert",
                "enabled": True,
                "warning_threshold_80": 180000000,
                "warning_threshold_90": 202500000,
            }

            result = index.lambda_handler(event, None)
            json.loads(result["body"])

            # JWT email takes priority
            mock_resolve.assert_called_once_with("jwt-user@company.com", [])
