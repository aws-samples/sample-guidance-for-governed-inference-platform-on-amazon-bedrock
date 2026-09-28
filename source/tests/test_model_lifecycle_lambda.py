# ABOUTME: Tests for the model_lifecycle Lambda's alert ladder and SSM-backed dedup
# ABOUTME: Date ladder (legacy/premium/EOL/absent), no-op when clean, reminder semantics

"""Tests for the model-lifecycle check Lambda (model-lifecycle stack).

The date ladder mirrors R14 §2.3 and uses the live-verified examples from
R14 §1.1 (us-east-1, 2026-07-08): sonnet-4 enters provider-set premium
pricing on 2026-07-14; opus-4-1 entered Legacy on 2026-07-08.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

LAMBDA_PATH = (
    Path(__file__).resolve().parents[2]
    / "deployment"
    / "infrastructure"
    / "lambda-functions"
    / "model_lifecycle"
    / "index.py"
)

NOW = datetime(2026, 7, 9, tzinfo=timezone.utc)

SONNET_4_LIFECYCLE = {
    "status": "LEGACY",
    "legacyTime": "2026-04-14T00:00:00Z",
    "publicExtendedAccessTime": "2026-07-14T00:00:00Z",
    "endOfLifeTime": "2026-10-14T00:00:00Z",
}

OPUS_4_1_LIFECYCLE = {
    "status": "LEGACY",
    "legacyTime": "2026-07-08T00:00:00Z",
    "publicExtendedAccessTime": "2026-10-08T00:00:00Z",
    "endOfLifeTime": "2027-01-08T00:00:00Z",
}


def _load_module(env: dict) -> object:
    """Load the Lambda module fresh with the given environment."""
    defaults = {
        "AWS_DEFAULT_REGION": "us-east-1",
        "TRACKED_MODELS_PARAM": "/gip/test/tracked-models",
        "ALERT_STATE_PARAM": "/gip/test/lifecycle-alert-state",
        "SNS_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:test-lifecycle-alerts",
        "LEGACY_PREMIUM_WARNING_DAYS": "30",
        "EOL_WARNING_DAYS": "60,30,7",
        "CHECK_REGIONS": "us-east-1",
        "REMINDER_DAYS": "7",
    }
    defaults.update(env)
    for key, value in defaults.items():
        os.environ[key] = value

    module_name = f"model_lifecycle_index_{id(env)}"
    spec = importlib.util.spec_from_file_location(module_name, LAMBDA_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _wire(mod, tracked: list[str], live: dict, state: dict | None = None, failed_regions: list | None = None):
    """Patch AWS clients: SSM returns tracked/state params, bedrock listing is stubbed.

    Returns (sns_mock, ssm_mock).
    """
    params = {
        mod.TRACKED_MODELS_PARAM: json.dumps(tracked),
        mod.ALERT_STATE_PARAM: json.dumps(state or {}),
    }

    ssm = MagicMock()
    ssm.exceptions.ParameterNotFound = type("ParameterNotFound", (Exception,), {})
    ssm.get_parameter.side_effect = lambda Name: {"Parameter": {"Value": params[Name]}}
    mod.ssm_client = ssm

    sns = MagicMock()
    mod.sns_client = sns

    mod.fetch_live_lifecycles = MagicMock(return_value=(live, failed_regions or []))
    return sns, ssm


def _fixed_now(mod, now=NOW):
    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    mod.datetime = _FixedDatetime


@pytest.fixture
def mod():
    return _load_module({})


class TestBaseModelId:
    def test_strips_cris_geography_prefix(self, mod):
        assert mod.base_model_id("us.anthropic.claude-sonnet-4-20250514-v1:0") == (
            "anthropic.claude-sonnet-4-20250514-v1:0"
        )
        assert mod.base_model_id("global.anthropic.claude-fable-5") == "anthropic.claude-fable-5"

    def test_base_id_passthrough(self, mod):
        assert mod.base_model_id("anthropic.claude-opus-4-1-20250805-v1:0") == (
            "anthropic.claude-opus-4-1-20250805-v1:0"
        )

    def test_match_lifecycle_tolerates_context_window_suffix(self, mod):
        live = {"anthropic.claude-fable-5:0:200k": {"status": "ACTIVE"}}
        assert mod.match_lifecycle("anthropic.claude-fable-5", live) == {"status": "ACTIVE"}
        assert mod.match_lifecycle("anthropic.claude-fable-50", live) is None


class TestDateLadder:
    """Pure evaluation of evaluate_tracked_model against R14's live-verified dates."""

    def test_active_model_no_alerts(self, mod):
        assert mod.evaluate_tracked_model("us.anthropic.claude-fable-5", {"status": "ACTIVE"}, NOW) == []

    def test_opus_4_1_legacy_only(self, mod):
        """Legacy entered 2026-07-08; premium (2026-10-08) is >30 days out at NOW."""
        alerts = mod.evaluate_tracked_model("us.anthropic.claude-opus-4-1-20250805-v1:0", OPUS_4_1_LIFECYCLE, NOW)
        assert [(k, s) for k, s, _m in alerts] == [("legacy", "WARNING")]
        assert "2026-07-08" in alerts[0][2]

    def test_sonnet_4_premium_within_30_days(self, mod):
        """Premium starts 2026-07-14 — 5 days from NOW → legacy + premium-30 warnings."""
        alerts = mod.evaluate_tracked_model("us.anthropic.claude-sonnet-4-20250514-v1:0", SONNET_4_LIFECYCLE, NOW)
        keys = [k for k, _s, _m in alerts]
        assert keys == ["legacy", "premium-30"]
        assert all(s == "WARNING" for _k, s, _m in alerts)
        assert "2026-07-14" in alerts[1][2]

    def test_premium_active_after_start(self, mod):
        now = datetime(2026, 8, 1, tzinfo=timezone.utc)
        alerts = mod.evaluate_tracked_model("m", SONNET_4_LIFECYCLE, now)
        assert ("premium-active", "WARNING") in [(k, s) for k, s, _m in alerts]

    @pytest.mark.parametrize(
        "now,expected_key",
        [
            (datetime(2026, 8, 20, tzinfo=timezone.utc), "eol-60"),  # 55 days to EOL
            (datetime(2026, 9, 20, tzinfo=timezone.utc), "eol-30"),  # 24 days to EOL
            (datetime(2026, 10, 10, tzinfo=timezone.utc), "eol-7"),  # 4 days to EOL
        ],
    )
    def test_eol_countdown_escalates_tightest_window_wins(self, mod, now, expected_key):
        alerts = mod.evaluate_tracked_model("m", SONNET_4_LIFECYCLE, now)
        eol_alerts = [(k, s) for k, s, _m in alerts if k.startswith("eol-")]
        assert eol_alerts == [(expected_key, "CRITICAL")]

    def test_absent_model_is_critical(self, mod):
        alerts = mod.evaluate_tracked_model("us.anthropic.claude-3-7-sonnet-20250219-v1:0", None, NOW)
        assert [(k, s) for k, s, _m in alerts] == [("absent", "CRITICAL")]
        assert "inference requests will fail" in alerts[0][2]

    def test_missing_premium_date_tolerated(self, mod):
        """publicExtendedAccessTime is optional on LEGACY models (R14 §1.1)."""
        lifecycle = {"status": "LEGACY", "legacyTime": "2026-01-30T00:00:00Z", "endOfLifeTime": "2026-07-30T00:00:00Z"}
        alerts = mod.evaluate_tracked_model("m", lifecycle, NOW)
        keys = [k for k, _s, _m in alerts]
        assert "legacy" in keys
        assert "eol-30" in keys  # 21 days out
        assert not any(k.startswith("premium") for k in keys)


class TestHandler:
    def test_noop_when_all_tracked_models_active(self, mod):
        _fixed_now(mod)
        sns, ssm = _wire(
            mod,
            tracked=["us.anthropic.claude-fable-5"],
            live={"anthropic.claude-fable-5": {"status": "ACTIVE"}},
        )
        result = mod.lambda_handler({}, None)
        assert result["clean"] is True
        assert result["alerts_sent"] == 0
        sns.publish.assert_not_called()

    def test_unseeded_parameter_is_noop_with_note(self, mod):
        _fixed_now(mod)
        sns, _ssm = _wire(mod, tracked=[], live={})
        result = mod.lambda_handler({}, None)
        assert result["tracked"] == 0
        assert "empty" in result["note"]
        sns.publish.assert_not_called()
        mod.fetch_live_lifecycles.assert_not_called()

    def test_legacy_model_publishes_and_records_state(self, mod):
        _fixed_now(mod)
        tracked_id = "us.anthropic.claude-sonnet-4-20250514-v1:0"
        sns, ssm = _wire(
            mod,
            tracked=[tracked_id],
            live={"anthropic.claude-sonnet-4-20250514-v1:0": SONNET_4_LIFECYCLE},
        )
        result = mod.lambda_handler({}, None)
        assert result["alerts_sent"] == 2  # legacy + premium-30
        assert result["clean"] is False
        subjects = [c.kwargs["Subject"] for c in sns.publish.call_args_list]
        assert any("WARNING" in s and tracked_id in s for s in subjects)
        saved_state = json.loads(ssm.put_parameter.call_args.kwargs["Value"])
        assert set(saved_state[tracked_id]) == {"legacy", "premium-30"}

    def test_already_alerted_within_reminder_window_not_resent(self, mod):
        _fixed_now(mod)
        tracked_id = "us.anthropic.claude-opus-4-1-20250805-v1:0"
        recent = datetime(2026, 7, 6, tzinfo=timezone.utc).isoformat()  # 3 days ago < 7
        sns, _ssm = _wire(
            mod,
            tracked=[tracked_id],
            live={"anthropic.claude-opus-4-1-20250805-v1:0": OPUS_4_1_LIFECYCLE},
            state={tracked_id: {"legacy": recent}},
        )
        result = mod.lambda_handler({}, None)
        assert result["alerts_sent"] == 0
        assert result["active_alert_keys"] == 1  # condition persists, just deduplicated
        sns.publish.assert_not_called()

    def test_reminder_resent_after_window(self, mod):
        _fixed_now(mod)
        tracked_id = "us.anthropic.claude-opus-4-1-20250805-v1:0"
        stale = datetime(2026, 6, 25, tzinfo=timezone.utc).isoformat()  # 14 days ago >= 7
        sns, _ssm = _wire(
            mod,
            tracked=[tracked_id],
            live={"anthropic.claude-opus-4-1-20250805-v1:0": OPUS_4_1_LIFECYCLE},
            state={tracked_id: {"legacy": stale}},
        )
        result = mod.lambda_handler({}, None)
        assert result["alerts_sent"] == 1
        sns.publish.assert_called_once()

    def test_absent_model_publishes_critical(self, mod):
        _fixed_now(mod)
        tracked_id = "us.anthropic.claude-3-7-sonnet-20250219-v1:0"
        sns, _ssm = _wire(mod, tracked=[tracked_id], live={"anthropic.claude-fable-5": {"status": "ACTIVE"}})
        result = mod.lambda_handler({}, None)
        assert result["alerts_sent"] == 1
        assert "CRITICAL" in sns.publish.call_args.kwargs["Subject"]

    def test_absent_suppressed_when_a_region_failed(self, mod):
        """A region we could not query might still list the model — no false CRITICAL."""
        _fixed_now(mod)
        tracked_id = "us.anthropic.claude-3-7-sonnet-20250219-v1:0"
        sns, _ssm = _wire(mod, tracked=[tracked_id], live={}, failed_regions=["us-west-2"])
        result = mod.lambda_handler({}, None)
        assert result["alerts_sent"] == 0
        assert result["failed_regions"] == ["us-west-2"]
        sns.publish.assert_not_called()


class TestFetchLiveLifecycles:
    def test_all_regions_failing_raises_for_a5_alarm(self, mod, monkeypatch):
        """Total failure must raise → Lambda Errors metric → alarm (fail-visible, A5)."""

        def broken_client(*args, **kwargs):
            raise RuntimeError("endpoint down")

        monkeypatch.setattr(mod.boto3, "client", broken_client)
        with pytest.raises(RuntimeError, match="ALL checked regions"):
            mod.fetch_live_lifecycles(["us-east-1", "us-west-2"])

    def test_partial_failure_returns_failed_regions(self, mod, monkeypatch):
        good = MagicMock()
        good.list_foundation_models.return_value = {
            "modelSummaries": [{"modelId": "anthropic.claude-fable-5", "modelLifecycle": {"status": "ACTIVE"}}]
        }

        def client(_service, region_name=None):
            if region_name == "us-west-2":
                raise RuntimeError("endpoint down")
            return good

        monkeypatch.setattr(mod.boto3, "client", client)
        live, failed = mod.fetch_live_lifecycles(["us-east-1", "us-west-2"])
        assert failed == ["us-west-2"]
        assert live["anthropic.claude-fable-5"]["status"] == "ACTIVE"

    def test_legacy_record_preferred_across_regions(self, mod, monkeypatch):
        responses = {
            "us-east-1": {"modelSummaries": [{"modelId": "m", "modelLifecycle": {"status": "ACTIVE"}}]},
            "us-west-2": {"modelSummaries": [{"modelId": "m", "modelLifecycle": SONNET_4_LIFECYCLE}]},
        }
        clients = {}
        for region, resp in responses.items():
            c = MagicMock()
            c.list_foundation_models.return_value = resp
            clients[region] = c
        monkeypatch.setattr(mod.boto3, "client", lambda _s, region_name=None: clients[region_name])
        live, failed = mod.fetch_live_lifecycles(["us-east-1", "us-west-2"])
        assert failed == []
        assert live["m"]["status"] == "LEGACY"
