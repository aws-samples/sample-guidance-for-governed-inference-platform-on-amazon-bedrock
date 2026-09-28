# ABOUTME: Tests that 'gip quota set-*' routes cost limits through QuotaPolicyManager
# ABOUTME: Regression: cost limits were written via a raw DynamoDB UpdateExpression, bypassing the manager

"""Cost-limit writes must go through manager.create_policy/update_policy.

Regression: quota.py had a `_write_cost_limits` helper that wrote
monthly_cost_limit/daily_cost_limit straight to DynamoDB with a raw
UpdateExpression, bypassing QuotaPolicyManager (no updated_at, no existence
condition, and drift from the QuotaPolicy schema). The CLI contract stays the
same: an omitted cost flag means "no change" (None), an explicit 0 clears the
budget.
"""

from unittest.mock import MagicMock, patch

import pytest
from cleo.testers.application_tester import ApplicationTester

from governed_inference_platform.cli import create_application
from governed_inference_platform.quota_policies import PolicyAlreadyExistsError


def _mock_config(mock_config_cls):
    mock_config = MagicMock()
    mock_profile = MagicMock()
    mock_profile.aws_region = "us-west-2"
    mock_profile.enable_finegrained_quotas = True
    mock_config.active_profile = "default"
    mock_config.get_profile.return_value = mock_profile
    mock_config_cls.load.return_value = mock_config


def _mock_policy(monthly_token_limit=1_000_000_000):
    policy = MagicMock()
    policy.monthly_token_limit = monthly_token_limit
    policy.daily_token_limit = None
    policy.enforcement_mode = MagicMock(value="alert")
    policy.daily_enforcement_mode = MagicMock(value="alert")
    return policy


def _run(command: str, mock_manager) -> int:
    app = create_application()
    tester = ApplicationTester(app)
    tester.execute(command)
    return tester.status_code


class TestCostLimitsRoutedThroughManager:
    """create/update must receive the cost kwargs; no raw table writes."""

    @patch("governed_inference_platform.cli.commands.quota._get_quota_manager")
    @patch("governed_inference_platform.cli.commands.quota.Config")
    def test_create_user_passes_cost_limits(self, mock_config_cls, mock_get_manager):
        """set-user with --budget and --daily-budget 0 sends 50.0 / 0.0 to create_policy."""
        _mock_config(mock_config_cls)
        mock_manager = MagicMock()
        mock_manager.create_policy.return_value = _mock_policy()
        mock_get_manager.return_value = mock_manager

        status = _run(
            "quota set-user alice@company.com --monthly-limit 1B --budget 50 --daily-budget 0",
            mock_manager,
        )

        assert status == 0
        kwargs = mock_manager.create_policy.call_args.kwargs
        assert kwargs["monthly_cost_limit"] == 50.0
        assert kwargs["daily_cost_limit"] == 0.0
        mock_manager.table.update_item.assert_not_called()

    @patch("governed_inference_platform.cli.commands.quota._get_quota_manager")
    @patch("governed_inference_platform.cli.commands.quota.Config")
    def test_cost_only_user_has_no_token_cap(self, mock_config_cls, mock_get_manager):
        _mock_config(mock_config_cls)
        mock_manager = MagicMock()
        mock_manager.create_policy.return_value = _mock_policy(0)
        mock_get_manager.return_value = mock_manager

        status = _run("quota set-user alice@company.com --budget 50", mock_manager)

        assert status == 0
        kwargs = mock_manager.create_policy.call_args.kwargs
        assert kwargs["monthly_token_limit"] == 0
        assert kwargs["quota_mode"].value == "cost"
        assert kwargs["monthly_cost_limit"] == 50.0

    @patch("governed_inference_platform.cli.commands.quota._get_quota_manager")
    @patch("governed_inference_platform.cli.commands.quota.Config")
    def test_monthly_cost_limit_only_has_no_token_cap(self, mock_config_cls, mock_get_manager):
        _mock_config(mock_config_cls)
        mock_manager = MagicMock()
        mock_manager.create_policy.return_value = _mock_policy()
        mock_get_manager.return_value = mock_manager

        status = _run("quota set-user alice@company.com --monthly-cost-limit 25", mock_manager)

        assert status == 0
        kwargs = mock_manager.create_policy.call_args.kwargs
        assert kwargs["monthly_token_limit"] == 0
        assert kwargs["quota_mode"].value == "cost"


class TestFinegrainedQuotaGate:
    @patch("governed_inference_platform.cli.commands.quota._get_quota_manager")
    @patch("governed_inference_platform.cli.commands.quota.Config")
    def test_policy_mutations_fail_when_feature_is_not_deployed(self, mock_config_cls, mock_get_manager, tmp_path):
        _mock_config(mock_config_cls)
        mock_config_cls.load.return_value.get_profile.return_value.enable_finegrained_quotas = False

        commands = [
            "quota set-user alice@company.com --monthly-limit 1B",
            "quota set-group engineering --monthly-limit 1B",
            "quota set-default --monthly-limit 1B",
            "quota delete user alice@company.com --force",
        ]
        import_file = tmp_path / "policies.json"
        import_file.write_text('{"policies": []}', encoding="utf-8")
        commands.append(f"quota import {import_file}")
        for command in commands:
            assert _run(command, mock_get_manager.return_value) == 1

        mock_get_manager.assert_not_called()

    @patch("governed_inference_platform.cli.commands.quota._get_quota_manager")
    @patch("governed_inference_platform.cli.commands.quota.Config")
    def test_list_remains_honest_when_feature_is_disabled(self, mock_config_cls, mock_get_manager):
        _mock_config(mock_config_cls)
        mock_config_cls.load.return_value.get_profile.return_value.enable_finegrained_quotas = False
        mock_get_manager.return_value.list_policies.return_value = []

        assert _run("quota list", mock_get_manager.return_value) == 0
        mock_get_manager.return_value.list_policies.assert_called_once_with(None)

    @patch("governed_inference_platform.cli.commands.quota._get_quota_manager")
    @patch("governed_inference_platform.cli.commands.quota.Config")
    def test_update_user_passes_cost_limits(self, mock_config_cls, mock_get_manager):
        """Existing policy: cost limits flow through update_policy, not raw DDB."""
        _mock_config(mock_config_cls)
        mock_manager = MagicMock()
        mock_manager.create_policy.side_effect = PolicyAlreadyExistsError("exists")
        mock_manager.update_policy.return_value = _mock_policy()
        mock_get_manager.return_value = mock_manager

        status = _run(
            "quota set-user alice@company.com --monthly-limit 1B --budget 50 --daily-budget 0",
            mock_manager,
        )

        assert status == 0
        kwargs = mock_manager.update_policy.call_args.kwargs
        assert kwargs["monthly_cost_limit"] == 50.0
        assert kwargs["daily_cost_limit"] == 0.0
        mock_manager.table.update_item.assert_not_called()

    @pytest.mark.parametrize(
        "command",
        [
            "quota set-user alice@company.com --budget 50",
            "quota set-group engineering --budget 50",
            "quota set-default --budget 50",
        ],
    )
    @patch("governed_inference_platform.cli.commands.quota._get_quota_manager")
    @patch("governed_inference_platform.cli.commands.quota.Config")
    def test_cost_only_update_clears_existing_daily_token_cap(self, mock_config_cls, mock_get_manager, command):
        _mock_config(mock_config_cls)
        mock_manager = MagicMock()
        mock_manager.create_policy.side_effect = PolicyAlreadyExistsError("exists")
        mock_manager.update_policy.return_value = _mock_policy(0)
        mock_get_manager.return_value = mock_manager

        assert _run(command, mock_manager) == 0
        kwargs = mock_manager.update_policy.call_args.kwargs
        assert kwargs["monthly_token_limit"] == 0
        assert kwargs["daily_token_limit"] == 0
        assert kwargs["quota_mode"].value == "cost"

    @patch("governed_inference_platform.cli.commands.quota._get_quota_manager")
    @patch("governed_inference_platform.cli.commands.quota.Config")
    def test_no_cost_flags_means_no_change(self, mock_config_cls, mock_get_manager):
        """Without cost flags, None is passed (update_policy treats None as no change)."""
        _mock_config(mock_config_cls)
        mock_manager = MagicMock()
        mock_manager.create_policy.side_effect = PolicyAlreadyExistsError("exists")
        mock_manager.update_policy.return_value = _mock_policy()
        mock_get_manager.return_value = mock_manager

        status = _run("quota set-user alice@company.com --monthly-limit 1B", mock_manager)

        assert status == 0
        kwargs = mock_manager.update_policy.call_args.kwargs
        assert kwargs["monthly_cost_limit"] is None
        assert kwargs["daily_cost_limit"] is None
        mock_manager.table.update_item.assert_not_called()

    @patch("governed_inference_platform.cli.commands.quota._get_quota_manager")
    @patch("governed_inference_platform.cli.commands.quota.Config")
    def test_budget_zero_alone_is_no_change(self, mock_config_cls, mock_get_manager):
        """--budget 0 with no other cost flag keeps the historical no-write gate."""
        _mock_config(mock_config_cls)
        mock_manager = MagicMock()
        mock_manager.create_policy.side_effect = PolicyAlreadyExistsError("exists")
        mock_manager.update_policy.return_value = _mock_policy()
        mock_get_manager.return_value = mock_manager

        status = _run("quota set-user alice@company.com --monthly-limit 1B --budget 0", mock_manager)

        assert status == 0
        kwargs = mock_manager.update_policy.call_args.kwargs
        assert kwargs["monthly_cost_limit"] is None
        assert kwargs["daily_cost_limit"] is None

    @patch("governed_inference_platform.cli.commands.quota._get_quota_manager")
    @patch("governed_inference_platform.cli.commands.quota.Config")
    def test_group_cost_limits_routed(self, mock_config_cls, mock_get_manager):
        """set-group routes --budget through create_policy (daily cleared to 0)."""
        _mock_config(mock_config_cls)
        mock_manager = MagicMock()
        mock_manager.create_policy.return_value = _mock_policy()
        mock_get_manager.return_value = mock_manager

        status = _run("quota set-group engineering --monthly-limit 1B --budget 200", mock_manager)

        assert status == 0
        kwargs = mock_manager.create_policy.call_args.kwargs
        assert kwargs["monthly_cost_limit"] == 200.0
        assert kwargs["daily_cost_limit"] == 0.0
        mock_manager.table.update_item.assert_not_called()

    @patch("governed_inference_platform.cli.commands.quota._get_quota_manager")
    @patch("governed_inference_platform.cli.commands.quota.Config")
    def test_group_cost_only_has_no_token_cap(self, mock_config_cls, mock_get_manager):
        _mock_config(mock_config_cls)
        mock_manager = MagicMock()
        mock_manager.create_policy.return_value = _mock_policy(0)
        mock_get_manager.return_value = mock_manager

        assert _run("quota set-group engineering --budget 200", mock_manager) == 0
        kwargs = mock_manager.create_policy.call_args.kwargs
        assert kwargs["monthly_token_limit"] == 0
        assert kwargs["quota_mode"].value == "cost"

    @patch("governed_inference_platform.cli.commands.quota._get_quota_manager")
    @patch("governed_inference_platform.cli.commands.quota.Config")
    def test_default_cost_limits_routed(self, mock_config_cls, mock_get_manager):
        """set-default routes --budget/--daily-budget through create_policy."""
        _mock_config(mock_config_cls)
        mock_manager = MagicMock()
        mock_manager.create_policy.return_value = _mock_policy()
        mock_get_manager.return_value = mock_manager

        status = _run("quota set-default --monthly-limit 1B --budget 30 --daily-budget 5", mock_manager)

        assert status == 0
        kwargs = mock_manager.create_policy.call_args.kwargs
        assert kwargs["monthly_cost_limit"] == 30.0
        assert kwargs["daily_cost_limit"] == 5.0
        mock_manager.table.update_item.assert_not_called()

    @patch("governed_inference_platform.cli.commands.quota._get_quota_manager")
    @patch("governed_inference_platform.cli.commands.quota.Config")
    def test_default_cost_only_has_no_token_cap(self, mock_config_cls, mock_get_manager):
        _mock_config(mock_config_cls)
        mock_manager = MagicMock()
        mock_manager.create_policy.return_value = _mock_policy(0)
        mock_get_manager.return_value = mock_manager

        assert _run("quota set-default --budget 30", mock_manager) == 0
        kwargs = mock_manager.create_policy.call_args.kwargs
        assert kwargs["monthly_token_limit"] == 0
        assert kwargs["quota_mode"].value == "cost"


class TestRawPathRemoved:
    """The raw UpdateExpression helper must be gone from the CLI module."""

    def test_write_cost_limits_helper_removed(self):
        import governed_inference_platform.cli.commands.quota as quota_module

        assert not hasattr(quota_module, "_write_cost_limits"), (
            "_write_cost_limits raw DynamoDB path must be removed; cost writes go through QuotaPolicyManager"
        )
