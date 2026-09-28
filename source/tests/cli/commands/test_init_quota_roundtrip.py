# ABOUTME: Regression test ensuring re-running init preserves all saved quota fields
# ABOUTME: Guards _check_existing_deployment against round-trip drift with _save_configuration

"""Regression test: re-running `gip init` must preserve saved quota settings.

`_check_existing_deployment` rebuilds the in-memory config dict from a saved
Profile when init is re-run. It must mirror every quota field that
`_save_configuration` persists — otherwise omitted fields (e.g.
quota_check_interval) silently reset to their prompt defaults on a re-run.
"""

import sys
from pathlib import Path
from unittest.mock import patch

# ruff: noqa: E402
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from governed_inference_platform.cli.commands.init import InitCommand
from governed_inference_platform.config import Config, Profile


def _make_profile() -> Profile:
    """A profile with non-default quota settings, as if previously saved."""
    return Profile(
        name="test",
        provider_domain="example.okta.com",
        client_id="0oa0000000000000000",
        identity_pool_name="gip-auth",
        credential_storage="keyring",
        aws_region="us-east-1",
        quota_monitoring_enabled=True,
        quota_limit_type="cost",
        monthly_cost_limit=75.0,
        daily_cost_limit=8.5,
        monthly_token_limit=500_000_000,
        warning_threshold_80=400_000_000,
        warning_threshold_90=450_000_000,
        daily_token_limit=20_000_000,
        burst_buffer_percent=15,
        daily_enforcement_mode="block",
        monthly_enforcement_mode="alert",
        quota_check_interval=5,
        enable_bypass_detection=True,
    )


def _rebuild_config(profile: Profile) -> dict:
    """Run _check_existing_deployment with AWS interaction stubbed out."""
    command = InitCommand()
    fake_config = Config()
    with (
        patch.object(Config, "load", return_value=fake_config),
        patch.object(fake_config, "get_profile", return_value=profile),
        # Avoid any AWS calls; pretend the stack check could not run.
        patch.object(InitCommand, "_stack_exists", side_effect=Exception("no creds")),
    ):
        return command._check_existing_deployment("test")


def test_rerun_preserves_quota_check_interval():
    """quota_check_interval must survive the profile -> config rebuild."""
    rebuilt = _rebuild_config(_make_profile())

    quota = rebuilt["quota"]
    # The dict uses "check_interval"; the Profile attribute is "quota_check_interval".
    assert quota["check_interval"] == 5


def test_rerun_preserves_all_quota_fields():
    """Every quota field _save_configuration writes must be rebuilt."""
    rebuilt = _rebuild_config(_make_profile())

    quota = rebuilt["quota"]
    assert quota["enabled"] is True
    assert quota["monthly_limit"] == 500_000_000
    assert quota["warning_threshold_80"] == 400_000_000
    assert quota["warning_threshold_90"] == 450_000_000
    assert quota["daily_limit"] == 20_000_000
    assert quota["burst_buffer_percent"] == 15
    assert quota["daily_enforcement_mode"] == "block"
    assert quota["monthly_enforcement_mode"] == "alert"
    assert quota["check_interval"] == 5
    assert quota["enable_bypass_detection"] is True


def test_rerun_preserves_cost_mode_fields():
    """Cost-mode settings must survive a re-run (regression: they were dropped,

    so a re-run of `gip init` silently reset a cost-based deployment back to
    token mode with the prompt defaults)."""
    rebuilt = _rebuild_config(_make_profile())

    quota = rebuilt["quota"]
    assert quota["limit_type"] == "cost"
    assert quota["monthly_cost_limit"] == 75.0
    assert quota["daily_cost_limit"] == 8.5


def test_rerun_derives_monthly_limit_millions():
    """The token-limit prompt default reads "monthly_limit_millions", but

    _save_configuration only persists "monthly_limit" (raw tokens). The rebuild
    must derive the millions value, or a re-run's prompt default silently
    reverts to 225 even when a different limit was saved."""
    rebuilt = _rebuild_config(_make_profile())

    quota = rebuilt["quota"]
    assert quota["monthly_limit"] == 500_000_000
    assert quota["monthly_limit_millions"] == 500
