# ABOUTME: Unit tests for quota_policies.py — token formatting, parsing, policy CRUD
# ABOUTME: Covers QuotaPolicyManager methods with mocked DynamoDB

"""Tests for governed_inference_platform.quota_policies module."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from botocore.exceptions import ClientError

from governed_inference_platform.quota_policies import (
    PolicyAlreadyExistsError,
    QuotaPolicyManager,
    _format_tokens,
    _parse_tokens,
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


def _load_quota_template() -> dict:
    path = Path(__file__).parents[2] / "deployment" / "infrastructure" / "quota-monitoring.yaml"
    with open(path, encoding="utf-8") as template_file:
        return yaml.load(template_file, Loader=_CfnLoader)  # nosec B506


class TestQuotaStateProtection:
    def test_monthly_token_limit_allows_zero_for_cost_only_mode(self):
        template = _load_quota_template()
        assert template["Parameters"]["MonthlyTokenLimit"]["MinValue"] == 0

    def test_durable_quota_tables_enable_point_in_time_recovery(self):
        template = _load_quota_template()
        for logical_id in ("QuotaPolicies", "UserQuotaMetrics"):
            properties = template["Resources"][logical_id]["Properties"]
            assert properties["PointInTimeRecoverySpecification"] == {"PointInTimeRecoveryEnabled": True}

    def test_durable_quota_tables_are_account_local_cell_tagged(self):
        template = _load_quota_template()
        for logical_id in ("QuotaPolicies", "UserQuotaMetrics"):
            tags = {tag["Key"]: tag["Value"] for tag in template["Resources"][logical_id]["Properties"]["Tags"]}
            assert tags["gip:service"] == "quota-enforcement"
            assert tags["gip:cell"] == "${AWS::AccountId}:${AWS::Region}"


class TestFormatTokens:
    """Tests for _format_tokens helper."""

    def test_billions(self):
        assert _format_tokens(1_000_000_000) == "1B"

    def test_billions_fractional(self):
        assert _format_tokens(1_500_000_000) == "1.5B"

    def test_millions(self):
        assert _format_tokens(300_000_000) == "300M"

    def test_millions_fractional(self):
        assert _format_tokens(2_500_000) == "2.5M"

    def test_thousands(self):
        assert _format_tokens(50_000) == "50K"

    def test_thousands_fractional(self):
        assert _format_tokens(1_500) == "1.5K"

    def test_small_number(self):
        assert _format_tokens(999) == "999"

    def test_zero(self):
        assert _format_tokens(0) == "0"


class TestParseTokens:
    """Tests for _parse_tokens helper."""

    def test_integer_passthrough(self):
        assert _parse_tokens(300_000_000) == 300_000_000

    def test_billions_suffix(self):
        assert _parse_tokens("1.5B") == 1_500_000_000

    def test_millions_suffix(self):
        assert _parse_tokens("300M") == 300_000_000

    def test_thousands_suffix(self):
        assert _parse_tokens("50K") == 50_000

    def test_lowercase_suffix(self):
        assert _parse_tokens("300m") == 300_000_000

    def test_plain_number_string(self):
        assert _parse_tokens("1000000") == 1_000_000

    def test_whitespace_stripped(self):
        assert _parse_tokens("  300M  ") == 300_000_000

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            _parse_tokens("not-a-number")


class TestQuotaPolicyManagerMakePk:
    """Tests for _make_pk key generation."""

    @patch("governed_inference_platform.quota_policies.boto3")
    def test_user_policy_key(self, mock_boto3):
        from governed_inference_platform.models import PolicyType

        manager = QuotaPolicyManager("test-table", region="us-east-1")
        pk = manager._make_pk(PolicyType.USER, "alice@example.com")
        assert pk == "POLICY#user#alice@example.com"

    @patch("governed_inference_platform.quota_policies.boto3")
    def test_group_policy_key(self, mock_boto3):
        from governed_inference_platform.models import PolicyType

        manager = QuotaPolicyManager("test-table", region="us-east-1")
        pk = manager._make_pk(PolicyType.GROUP, "engineering")
        assert pk == "POLICY#group#engineering"

    @patch("governed_inference_platform.quota_policies.boto3")
    def test_default_policy_key(self, mock_boto3):
        from governed_inference_platform.models import PolicyType

        manager = QuotaPolicyManager("test-table", region="us-east-1")
        pk = manager._make_pk(PolicyType.DEFAULT, "default")
        assert pk == "POLICY#default#default"


class TestQuotaPolicyManagerCreatePolicy:
    """Tests for create_policy method."""

    @patch("governed_inference_platform.quota_policies.boto3")
    def test_create_policy_success(self, mock_boto3):
        from governed_inference_platform.models import PolicyType

        mock_table = MagicMock()
        mock_boto3.resource.return_value.Table.return_value = mock_table

        manager = QuotaPolicyManager("test-table", region="us-east-1")
        policy = manager.create_policy(
            policy_type=PolicyType.USER,
            identifier="alice@example.com",
            monthly_token_limit=300_000_000,
        )

        assert policy.identifier == "alice@example.com"
        assert policy.monthly_token_limit == 300 * 1_000_000
        assert policy.warning_threshold_80 == 240_000_000
        assert policy.warning_threshold_90 == 270_000_000
        mock_table.put_item.assert_called_once()

    @patch("governed_inference_platform.quota_policies.boto3")
    def test_create_policy_persists_daily_enforcement_mode(self, mock_boto3):
        """A daily-enforcement=block policy must persist daily_enforcement_mode so the
        check Lambda can hard-block a daily breach independently of the monthly mode."""
        from governed_inference_platform.models import EnforcementMode, PolicyType

        mock_table = MagicMock()
        mock_boto3.resource.return_value.Table.return_value = mock_table

        manager = QuotaPolicyManager("test-table", region="us-east-1")
        policy = manager.create_policy(
            policy_type=PolicyType.USER,
            identifier="alice@example.com",
            monthly_token_limit=300_000_000,
            daily_token_limit=1_000,
            enforcement_mode=EnforcementMode.ALERT,
            daily_enforcement_mode=EnforcementMode.BLOCK,
        )

        assert policy.daily_enforcement_mode == EnforcementMode.BLOCK
        # Default when unspecified stays alert (backward compatible).
        default_policy = manager.create_policy(
            policy_type=PolicyType.DEFAULT,
            identifier="default",
            monthly_token_limit=225_000_000,
        )
        assert default_policy.daily_enforcement_mode == EnforcementMode.ALERT
        # And it is written to the DynamoDB item.
        written_item = mock_table.put_item.call_args_list[0].kwargs["Item"]
        assert written_item["daily_enforcement_mode"] == "block"

    @patch("governed_inference_platform.quota_policies.boto3")
    def test_create_policy_already_exists(self, mock_boto3):
        from governed_inference_platform.models import PolicyType

        mock_table = MagicMock()
        mock_boto3.resource.return_value.Table.return_value = mock_table
        mock_table.put_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
            "PutItem",
        )

        manager = QuotaPolicyManager("test-table", region="us-east-1")
        with pytest.raises(PolicyAlreadyExistsError):
            manager.create_policy(
                policy_type=PolicyType.USER,
                identifier="alice@example.com",
                monthly_token_limit=300_000_000,
            )

    @patch("governed_inference_platform.quota_policies.boto3")
    def test_default_policy_forces_identifier(self, mock_boto3):
        from governed_inference_platform.models import PolicyType

        mock_table = MagicMock()
        mock_boto3.resource.return_value.Table.return_value = mock_table

        manager = QuotaPolicyManager("test-table", region="us-east-1")
        policy = manager.create_policy(
            policy_type=PolicyType.DEFAULT,
            identifier="anything",  # Should be overridden to "default"
            monthly_token_limit=100_000_000,
        )

        assert policy.identifier == "default"

    @patch("governed_inference_platform.quota_policies.boto3")
    def test_auto_calculate_thresholds(self, mock_boto3):
        from governed_inference_platform.models import PolicyType

        mock_table = MagicMock()
        mock_boto3.resource.return_value.Table.return_value = mock_table

        manager = QuotaPolicyManager("test-table", region="us-east-1")
        policy = manager.create_policy(
            policy_type=PolicyType.USER,
            identifier="bob@example.com",
            monthly_token_limit=1_000_000_000,
        )

        assert policy.warning_threshold_80 == 800_000_000
        assert policy.warning_threshold_90 == 900_000_000


class TestQuotaPolicyManagerGetPolicy:
    """Tests for get_policy method."""

    @patch("governed_inference_platform.quota_policies.boto3")
    def test_get_existing_policy(self, mock_boto3):
        from governed_inference_platform.models import PolicyType

        mock_table = MagicMock()
        mock_boto3.resource.return_value.Table.return_value = mock_table
        mock_table.get_item.return_value = {
            "Item": {
                "pk": "POLICY#user#alice@example.com",
                "sk": "CURRENT",
                "policy_type": "user",
                "identifier": "alice@example.com",
                "monthly_token_limit": 300_000_000,
                "daily_token_limit": None,
                "warning_threshold_80": 240_000_000,
                "warning_threshold_90": 270_000_000,
                "enforcement_mode": "alert",
                "enabled": True,
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
            }
        }

        manager = QuotaPolicyManager("test-table", region="us-east-1")
        policy = manager.get_policy(PolicyType.USER, "alice@example.com")
        assert policy is not None
        assert policy.monthly_token_limit == 300 * 1_000_000

    @patch("governed_inference_platform.quota_policies.boto3")
    def test_get_nonexistent_policy(self, mock_boto3):
        from governed_inference_platform.models import PolicyType

        mock_table = MagicMock()
        mock_boto3.resource.return_value.Table.return_value = mock_table
        mock_table.get_item.return_value = {}  # No Item key

        manager = QuotaPolicyManager("test-table", region="us-east-1")
        policy = manager.get_policy(PolicyType.USER, "ghost@example.com")
        assert policy is None


class TestQuotaPolicyCostLimits:
    """Cost limits are first-class QuotaPolicy schema fields (regression).

    Previously USD budgets were written to DynamoDB as raw attributes outside
    the dataclass, so get_policy/export/import silently dropped them and the
    check Lambda read attribute names that CRUD never wrote (issue #748 class
    of bugs)."""

    def test_dataclass_dynamodb_roundtrip(self):
        from decimal import Decimal

        from governed_inference_platform.models import PolicyType, QuotaMode, QuotaPolicy

        policy = QuotaPolicy(
            policy_type=PolicyType.USER,
            identifier="alice@example.com",
            monthly_token_limit=0,
            quota_mode=QuotaMode.COST,
            monthly_cost_limit=50.0,
            daily_cost_limit=7.5,
        )

        item = policy.to_dynamodb_item()
        # DynamoDB rejects float; must serialize as Decimal
        assert item["monthly_cost_limit"] == Decimal("50.0")
        assert item["daily_cost_limit"] == Decimal("7.5")

        restored = QuotaPolicy.from_dynamodb_item(item)
        assert restored.monthly_cost_limit == 50.0
        assert restored.daily_cost_limit == 7.5
        assert restored.quota_mode == QuotaMode.COST

    def test_legacy_cost_policy_infers_cost_only_and_discards_synthetic_cap(self):
        from governed_inference_platform.models import QuotaMode, QuotaPolicy

        restored = QuotaPolicy.from_dynamodb_item(
            {
                "policy_type": "user",
                "identifier": "alice@example.com",
                "monthly_token_limit": 1_000_000_000,
                "monthly_cost_limit": 50,
            }
        )

        assert restored.quota_mode == QuotaMode.COST
        assert restored.monthly_token_limit == 0

    def test_unset_cost_limits_omitted_from_item(self):
        from governed_inference_platform.models import PolicyType, QuotaPolicy

        policy = QuotaPolicy(
            policy_type=PolicyType.USER,
            identifier="alice@example.com",
            monthly_token_limit=300_000_000,
        )
        item = policy.to_dynamodb_item()
        assert "monthly_cost_limit" not in item
        assert "daily_cost_limit" not in item

    @patch("governed_inference_platform.quota_policies.boto3")
    def test_create_policy_persists_cost_limits(self, mock_boto3):
        from decimal import Decimal

        from governed_inference_platform.models import PolicyType

        mock_table = MagicMock()
        mock_boto3.resource.return_value.Table.return_value = mock_table

        manager = QuotaPolicyManager("test-table", region="us-east-1")
        manager.create_policy(
            policy_type=PolicyType.USER,
            identifier="alice@example.com",
            monthly_token_limit=0,
            monthly_cost_limit=100.0,
            daily_cost_limit=10.0,
        )

        written = mock_table.put_item.call_args.kwargs["Item"]
        assert written["quota_mode"] == "cost"
        assert written["monthly_cost_limit"] == Decimal("100.0")
        assert written["daily_cost_limit"] == Decimal("10.0")

    @patch("governed_inference_platform.quota_policies.boto3")
    def test_export_includes_cost_limits(self, mock_boto3):
        from governed_inference_platform.models import PolicyType, QuotaPolicy

        mock_table = MagicMock()
        mock_boto3.resource.return_value.Table.return_value = mock_table

        manager = QuotaPolicyManager("test-table", region="us-east-1")
        policy = QuotaPolicy(
            policy_type=PolicyType.USER,
            identifier="alice@example.com",
            monthly_token_limit=0,
            monthly_cost_limit=50.0,
        )
        with patch.object(manager, "list_policies", return_value=[policy]):
            exported = manager.export_policies()

        assert exported[0]["monthly_cost_limit"] == 50.0
        assert exported[0]["daily_cost_limit"] == ""

    @patch("governed_inference_platform.quota_policies.boto3")
    def test_import_parses_cost_limits(self, mock_boto3):
        from governed_inference_platform.models import PolicyType

        mock_table = MagicMock()
        mock_boto3.resource.return_value.Table.return_value = mock_table

        manager = QuotaPolicyManager("test-table", region="us-east-1")
        parsed = manager._parse_import_policy(
            {
                "type": "user",
                "identifier": "alice@example.com",
                "monthly_token_limit": "0",
                "monthly_cost_limit": "50",
                "daily_cost_limit": "7.5",
            },
            row_num=1,
            auto_daily=False,
            burst_buffer_percent=10,
        )
        assert parsed["policy_type"] == PolicyType.USER
        assert parsed["monthly_cost_limit"] == 50.0
        assert parsed["daily_cost_limit"] == 7.5

    @patch("governed_inference_platform.quota_policies.boto3")
    def test_import_rejects_negative_cost(self, mock_boto3):
        mock_table = MagicMock()
        mock_boto3.resource.return_value.Table.return_value = mock_table

        manager = QuotaPolicyManager("test-table", region="us-east-1")
        with pytest.raises(ValueError, match="cannot be negative"):
            manager._parse_import_policy(
                {
                    "type": "user",
                    "identifier": "alice@example.com",
                    "monthly_token_limit": "0",
                    "monthly_cost_limit": "-5",
                },
                row_num=1,
                auto_daily=False,
                burst_buffer_percent=10,
            )
