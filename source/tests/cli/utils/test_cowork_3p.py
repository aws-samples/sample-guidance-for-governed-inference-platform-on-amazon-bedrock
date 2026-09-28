# ABOUTME: Tests for cowork_3p.py add_monitoring_config endpoint resolution
# ABOUTME: Covers stack output success, stack failure with profile fallback, and both missing

"""Tests for CoWork 3P monitoring configuration (endpoint resolution + auth headers)."""

import json
from unittest.mock import MagicMock, patch

import pytest

from governed_inference_platform.cli.utils.cowork_3p import (
    add_monitoring_config,
    build_mdm_config,
    telemetry_endpoint_allows_credentials,
)


class FakeProfile:
    """Minimal profile stub for testing add_monitoring_config."""

    def __init__(
        self,
        monitoring_enabled=True,
        monitoring_mode="central",
        otel_collector_endpoint=None,
        identity_pool_name="test-pool",
        aws_region="us-east-1",
        auth_type="oidc",
        quota_api_endpoint=None,
    ):
        self.monitoring_enabled = monitoring_enabled
        self.monitoring_mode = monitoring_mode
        self.otel_collector_endpoint = otel_collector_endpoint
        self.identity_pool_name = identity_pool_name
        self.aws_region = aws_region
        self.auth_type = auth_type
        self.quota_api_endpoint = quota_api_endpoint
        self.stack_names = {}


class TestAddMonitoringConfig:
    """Tests for add_monitoring_config endpoint resolution logic."""

    def _make_console(self):
        return MagicMock()

    def test_monitoring_disabled_skips(self):
        """When monitoring_enabled=False, nothing is set."""
        profile = FakeProfile(monitoring_enabled=False)
        mdm = {}
        add_monitoring_config(mdm, profile, self._make_console())
        assert "otlpEndpoint" not in mdm

    def test_sidecar_mode_uses_local_proxy(self):
        """Sidecar mode configures CoWork to send to localhost otel-helper proxy."""
        profile = FakeProfile(monitoring_mode="sidecar")
        mdm = {}
        add_monitoring_config(mdm, profile, self._make_console())
        assert mdm["otlpEndpoint"] == "http://localhost:4318"
        assert mdm["otlpProtocol"] == "http/protobuf"

    def test_sidecar_mode_without_cowork_token(self):
        """Sidecar mode without cowork_service_token omits otlpHeaders."""
        profile = FakeProfile(monitoring_mode="sidecar")
        profile.cowork_service_token = None
        mdm = {}
        add_monitoring_config(mdm, profile, self._make_console())
        assert "otlpHeaders" not in mdm

    def test_sidecar_none_mode_fails_visible_without_per_user_telemetry(self):
        """No-auth sidecar must not emit its stable pseudonymous ID as a user dimension."""
        profile = FakeProfile(monitoring_mode="sidecar", auth_type="none")
        mdm = {}
        console = self._make_console()

        add_monitoring_config(mdm, profile, console)

        assert "otlpEndpoint" not in mdm
        assert "otlpHeaders" not in mdm
        assert "aggregate-only" in str(console.print.call_args_list[-1])

    def test_sidecar_idc_mode_remains_available_with_iam_bound_helper(self):
        """IDC sidecar remains available when the binary can resolve IAM identity."""
        profile = FakeProfile(
            monitoring_mode="sidecar",
            auth_type="idc",
            quota_api_endpoint="https://quota.example.com/check",
        )
        mdm = {}

        add_monitoring_config(mdm, profile, self._make_console())

        assert mdm["otlpEndpoint"] == "http://localhost:4318"
        assert mdm["otlpProtocol"] == "http/protobuf"

    def test_sidecar_idc_zero_binary_fails_visible(self):
        """IDC without the IAM-bound helper must not claim per-user sidecar telemetry."""
        profile = FakeProfile(monitoring_mode="sidecar", auth_type="idc")
        mdm = {}
        console = self._make_console()

        add_monitoring_config(mdm, profile, console)

        assert "otlpEndpoint" not in mdm
        assert "otlpHeaders" not in mdm
        warning = str(console.print.call_args_list[-1])
        assert "zero-binary" in warning
        assert "IAM identity" in warning

    @patch("governed_inference_platform.cli.utils.cowork_3p.get_stack_outputs")
    def test_stack_output_success(self, mock_get_outputs):
        """When stack outputs resolve, endpoint is set from CollectorEndpoint."""
        mock_get_outputs.return_value = {"CollectorEndpoint": "https://telemetry.example.com"}
        profile = FakeProfile()
        mdm = {}
        add_monitoring_config(mdm, profile, self._make_console())
        assert mdm["otlpEndpoint"] == "https://telemetry.example.com"
        assert mdm["otlpProtocol"] == "http/protobuf"

    @patch("governed_inference_platform.cli.utils.cowork_3p.get_stack_outputs")
    def test_stack_failure_falls_back_to_profile(self, mock_get_outputs):
        """When stack query fails, falls back to profile.otel_collector_endpoint."""
        mock_get_outputs.side_effect = Exception("stack not found")
        profile = FakeProfile(otel_collector_endpoint="https://fallback.example.com")
        mdm = {}
        add_monitoring_config(mdm, profile, self._make_console())
        assert mdm["otlpEndpoint"] == "https://fallback.example.com"
        assert mdm["otlpProtocol"] == "http/protobuf"

    @patch("governed_inference_platform.cli.utils.cowork_3p.get_stack_outputs")
    def test_stack_returns_no_endpoint_falls_back_to_profile(self, mock_get_outputs):
        """When stack outputs exist but CollectorEndpoint is missing, use profile fallback."""
        mock_get_outputs.return_value = {"SomeOtherOutput": "value"}
        profile = FakeProfile(otel_collector_endpoint="https://profile-endpoint.example.com")
        mdm = {}
        add_monitoring_config(mdm, profile, self._make_console())
        assert mdm["otlpEndpoint"] == "https://profile-endpoint.example.com"

    @patch("governed_inference_platform.cli.utils.cowork_3p.get_stack_outputs")
    def test_both_missing_shows_warning(self, mock_get_outputs):
        """When stack query fails and no profile endpoint, no otlpEndpoint is set."""
        mock_get_outputs.side_effect = Exception("stack not found")
        profile = FakeProfile(otel_collector_endpoint=None)
        mdm = {}
        console = self._make_console()
        add_monitoring_config(mdm, profile, console)
        assert "otlpEndpoint" not in mdm
        # Should print a warning
        console.print.assert_called()
        warning_text = str(console.print.call_args_list[-1])
        assert "Could not resolve" in warning_text or "warning" in warning_text.lower()

    @patch("governed_inference_platform.cli.utils.cowork_3p.get_stack_outputs")
    def test_custom_stack_name_from_profile(self, mock_get_outputs):
        """Uses stack name from profile.stack_names if configured."""
        mock_get_outputs.return_value = {"CollectorEndpoint": "https://custom-stack.example.com"}
        profile = FakeProfile()
        profile.stack_names = {"monitoring": "my-custom-monitoring-stack"}
        mdm = {}
        add_monitoring_config(mdm, profile, self._make_console())
        mock_get_outputs.assert_called_once_with("my-custom-monitoring-stack", "us-east-1")
        assert mdm["otlpEndpoint"] == "https://custom-stack.example.com"

    @patch("governed_inference_platform.cli.utils.cowork_3p.get_stack_outputs")
    def test_cowork_service_token_adds_otlp_headers(self, mock_get_outputs):
        """When cowork_service_token is set, otlpHeaders includes X-Cowork-Token."""
        mock_get_outputs.return_value = {"CollectorEndpoint": "https://collector.example.com"}
        profile = FakeProfile()
        profile.cowork_service_token = "test-token-abc123"  # nosec B105 -- dummy token fixture, not a real credential
        mdm = {}
        console = self._make_console()
        add_monitoring_config(mdm, profile, console)
        assert "otlpHeaders" in mdm
        headers = json.loads(mdm["otlpHeaders"])
        assert headers == {"X-Cowork-Token": "test-token-abc123"}
        assert not any(key.lower().startswith(("authorization", "x-user-")) for key in headers)
        assert "aggregate-only" in str(console.print.call_args_list[-1])

    @patch("governed_inference_platform.cli.utils.cowork_3p.get_stack_outputs")
    def test_no_cowork_service_token_omits_otlp_headers(self, mock_get_outputs):
        """When cowork_service_token is not set, otlpHeaders is not added."""
        mock_get_outputs.return_value = {"CollectorEndpoint": "https://collector.example.com"}
        profile = FakeProfile()
        # No cowork_service_token attribute
        mdm = {}
        add_monitoring_config(mdm, profile, self._make_console())
        assert "otlpHeaders" not in mdm

    @patch("governed_inference_platform.cli.utils.cowork_3p.get_stack_outputs")
    def test_central_none_mode_uses_only_aggregate_ingress(self, mock_get_outputs):
        """No-auth central mode sends no identity or authorization headers."""
        mock_get_outputs.return_value = {"CollectorEndpoint": "http://collector.internal"}
        profile = FakeProfile(auth_type="none")
        mdm = {}
        console = self._make_console()

        add_monitoring_config(mdm, profile, console)

        assert mdm["otlpEndpoint"] == "http://collector.internal"
        assert "otlpHeaders" not in mdm
        warning = str(console.print.call_args_list[-1])
        assert "aggregate-only" in warning
        assert "quota" in warning

    @patch("governed_inference_platform.cli.utils.cowork_3p.get_stack_outputs")
    def test_central_idc_is_explicitly_aggregate_only(self, mock_get_outputs):
        """The ALB has no IAM/SigV4 ingress, so central IDC cannot be per-user."""
        mock_get_outputs.return_value = {"CollectorEndpoint": "https://collector.example.com"}
        profile = FakeProfile(auth_type="idc")
        mdm = {}
        console = self._make_console()

        add_monitoring_config(mdm, profile, console)

        assert mdm["otlpEndpoint"] == "https://collector.example.com"
        assert "otlpHeaders" not in mdm
        warning = str(console.print.call_args_list[-1])
        assert "aggregate-only" in warning
        assert "Per-user monitoring" in warning

    @patch("governed_inference_platform.cli.utils.cowork_3p.get_stack_outputs")
    def test_http_only_oidc_is_explicitly_aggregate_only(self, mock_get_outputs):
        """HTTP-only OIDC cannot reach the HTTPS JWT-validation listener."""
        mock_get_outputs.return_value = {"CollectorEndpoint": "http://collector.internal"}
        profile = FakeProfile(auth_type="oidc")
        mdm = {}
        console = self._make_console()

        add_monitoring_config(mdm, profile, console)

        assert mdm["otlpEndpoint"] == "http://collector.internal"
        assert "otlpHeaders" not in mdm
        assert "aggregate-only" in str(console.print.call_args_list[-1])

    @patch("governed_inference_platform.cli.utils.cowork_3p.get_stack_outputs")
    def test_remote_http_endpoint_never_receives_service_token(self, mock_get_outputs):
        mock_get_outputs.return_value = {"CollectorEndpoint": "http://collector.internal"}
        profile = FakeProfile()
        profile.cowork_service_token = "test-token-abc123"  # nosec B105 -- dummy token fixture, not a real credential
        mdm = {}
        console = self._make_console()

        add_monitoring_config(mdm, profile, console)

        assert mdm["otlpEndpoint"] == "http://collector.internal"
        assert "otlpHeaders" not in mdm
        assert "credentials are never sent" in str(console.print.call_args_list)

    @patch("governed_inference_platform.cli.utils.cowork_3p.get_stack_outputs")
    def test_remote_http_endpoint_removes_preexisting_custom_headers(self, mock_get_outputs):
        mock_get_outputs.return_value = {"CollectorEndpoint": "http://collector.internal"}
        profile = FakeProfile()
        mdm = {"otlpHeaders": '{"Authorization":"Bearer secret"}'}

        add_monitoring_config(mdm, profile, self._make_console())

        assert "otlpHeaders" not in mdm

    @patch("governed_inference_platform.cli.utils.cowork_3p.get_stack_outputs")
    def test_empty_cowork_service_token_omits_otlp_headers(self, mock_get_outputs):
        """When cowork_service_token is empty string, otlpHeaders is not added."""
        mock_get_outputs.return_value = {"CollectorEndpoint": "https://collector.example.com"}
        profile = FakeProfile()
        profile.cowork_service_token = ""  # nosec B105 -- empty string, not a credential
        mdm = {}
        add_monitoring_config(mdm, profile, self._make_console())
        assert "otlpHeaders" not in mdm


def test_telemetry_credentials_require_tls_or_loopback():
    assert telemetry_endpoint_allows_credentials("https://collector.example.com")
    assert telemetry_endpoint_allows_credentials("http://localhost:4318")
    assert telemetry_endpoint_allows_credentials("http://127.0.0.1:4318")
    assert telemetry_endpoint_allows_credentials("http://[::1]:4318")
    assert not telemetry_endpoint_allows_credentials("http://collector.internal")
    assert not telemetry_endpoint_allows_credentials("not-a-url")


def test_custom_mdm_keys_cannot_send_credentials_to_remote_http():
    with pytest.raises(ValueError, match="OTLP credentials require HTTPS"):
        build_mdm_config(
            "us-east-1",
            ["sonnet"],
            extra_keys={
                "otlpEndpoint": "http://collector.internal",
                "otlpHeaders": '{"Authorization":"Bearer secret"}',
            },
        )
