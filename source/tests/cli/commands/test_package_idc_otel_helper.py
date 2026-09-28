# ABOUTME: Regression tests for otelHeadersHelper wiring across auth types
# ABOUTME: Only trusted local or TLS-verified OIDC paths may wire the identity helper

"""Regression tests for identity-helper containment.

The central collector establishes per-user identity only on its TLS-protected OIDC
listener. IDC, no-auth, and HTTP-only central routes are aggregate-only even when a
credential-process binary exists, so they must not receive identity helper output.

Cases:
  - Central OIDC over HTTPS: helper wired (supplies the JWT).
  - Central OIDC over HTTP: helper not wired.
  - Central IDC: helper not wired.
  - Sidecar IDC with a binary: helper wired locally.
  - IDC zero-binary (no quota_api_endpoint): helper NOT wired — identity comes
    from the static collector config instead.
"""

import json
import tempfile
from pathlib import Path

from governed_inference_platform.cli.commands.package import PackageCommand
from governed_inference_platform.config import Profile


def _base(**overrides) -> Profile:
    kwargs = {
        "name": "test",
        "provider_domain": "test.okta.com",
        "client_id": "test-client-id",
        "credential_storage": "session",
        "aws_region": "us-east-1",
        "identity_pool_name": "test-pool",
        "allowed_bedrock_regions": ["us-east-1", "us-west-2"],
        "cross_region_profile": "us",
        "monitoring_enabled": True,
        "otel_collector_endpoint": "https://collector.example.com",
        "otel_verified_collector_endpoint": "https://collector.example.com",
        "stack_names": {"monitoring": "test-pool-otel-collector"},
    }
    kwargs.update(overrides)
    return Profile(**kwargs)


def _oidc_profile() -> Profile:
    return _base(auth_type="oidc", sso_enabled=True)


def _idc_with_binary_profile() -> Profile:
    # quota_api_endpoint set => credential-process binary is included.
    return _base(
        provider_domain="",
        client_id="",
        auth_type="idc",
        sso_enabled=False,
        idc_start_url="https://d-1234567890.awsapps.com/start",
        idc_account_id="123456789012",
        idc_permission_set_name="ClaudeCodeRole",
        quota_api_endpoint="https://quota.example.com/check",
    )


def _idc_sidecar_with_binary_profile() -> Profile:
    profile = _idc_with_binary_profile()
    profile.monitoring_mode = "sidecar"
    return profile


def _idc_zero_binary_profile() -> Profile:
    # No quota_api_endpoint => zero-binary IDC (static identity in collector).
    return _base(
        provider_domain="",
        client_id="",
        auth_type="idc",
        sso_enabled=False,
        idc_start_url="https://d-1234567890.awsapps.com/start",
        idc_account_id="123456789012",
        idc_permission_set_name="ClaudeCodeRole",
    )


def _read_settings(output_dir: Path) -> dict:
    with open(output_dir / "gip-settings" / "settings.json", encoding="utf-8") as f:
        return json.load(f)


def _generate(profile: Profile) -> dict:
    cmd = PackageCommand()
    with tempfile.TemporaryDirectory() as tmpdir:
        out = Path(tmpdir)
        cmd._create_claude_settings(out, profile, profile_name="test")
        return _read_settings(out)


class TestOtelHeadersHelperWiring:
    def test_oidc_wires_helper(self):
        settings = _generate(_oidc_profile())
        assert settings.get("otelHeadersHelper") == "__OTEL_HELPER_PATH__ --profile test"

    def test_central_idc_with_binary_does_not_wire_helper(self):
        settings = _generate(_idc_with_binary_profile())
        assert "otelHeadersHelper" not in settings

    def test_sidecar_idc_with_binary_wires_local_helper(self):
        settings = _generate(_idc_sidecar_with_binary_profile())
        assert settings.get("otelHeadersHelper") == "__OTEL_HELPER_PATH__ --profile test"

    def test_remote_http_oidc_does_not_wire_helper(self):
        profile = _oidc_profile()
        profile.otel_collector_endpoint = "http://collector.internal"
        settings = _generate(profile)
        assert "otelHeadersHelper" not in settings

    def test_sidecar_none_auth_does_not_wire_identity_helper(self):
        profile = _base(auth_type="none", monitoring_mode="sidecar")
        settings = _generate(profile)
        assert "otelHeadersHelper" not in settings

    def test_arbitrary_https_oidc_without_verified_output_is_aggregate_only(self):
        profile = _oidc_profile()
        profile.otel_verified_collector_endpoint = None
        settings = _generate(profile)
        assert "otelHeadersHelper" not in settings

    def test_idc_zero_binary_does_not_wire_helper(self):
        """Zero-binary IDC has no runtime identity resolver; attribution comes
        from the static collector config, so the helper must stay unset."""
        settings = _generate(_idc_zero_binary_profile())
        assert "otelHeadersHelper" not in settings
