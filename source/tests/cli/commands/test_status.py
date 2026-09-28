# ABOUTME: Tests gateway and inference-cell readiness reporting in gip status.
# ABOUTME: Covers stack discovery, output mapping, and truthful non-blocking warnings.

import json
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from rich.console import Console

from governed_inference_platform.cli.commands.status import StatusCommand, _gateway_readiness_warnings


def _profile(**overrides):
    values = {
        "name": "test",
        "identity_pool_name": "gip",
        "aws_region": "us-west-2",
        "stack_names": {},
        "sso_enabled": False,
        "monitoring_enabled": False,
        "gateway_enabled": False,
        "gateway_inference_profile_prefix": "us",
        "web_search_enabled": False,
        "websearch_region": None,
        "websearch_entitled_groups": [],
        "websearch_policy_mode": "LOG_ONLY",
        "auth_type": "oidc",
        "metering_enabled": False,
        "allowed_bedrock_regions": [],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_upstream_gateway_warning_defers_to_vendored_source():
    warnings = _gateway_readiness_warnings(_profile(), {"gateway_source": "aws-samples-upstream"})

    assert len(warnings) == 1
    assert "pinned AWS Samples implementation" in warnings[0]
    assert "does not reinterpret" in warnings[0]


def test_legacy_gateway_profile_warns_without_inferred_posture():
    warnings = _gateway_readiness_warnings(_profile(gateway_enabled=True), {})

    assert len(warnings) == 1
    assert "legacy GIP gateway fields" in warnings[0]
    assert "remove old stacks" in warnings[0]


def test_agentcore_log_only_and_missing_production_mode_are_warnings():
    profile = _profile(
        web_search_enabled=True,
        websearch_entitled_groups=["developers"],
        websearch_policy_mode="LOG_ONLY",
    )
    endpoints = {
        "agentcore_deployment_mode": "development",
        "agentcore_production_readiness": "DEVELOPMENT_DEFAULTS",
        "agentcore_entitlement_policy_status": "LOG_ONLY",
        "agentcore_policy_validation_status": "NOT_ACKNOWLEDGED",
        "agentcore_authorizer_type": "CUSTOM_JWT",
    }

    warnings = _gateway_readiness_warnings(profile, endpoints)
    assert any("development defaults" in warning for warning in warnings)
    assert any("LOG_ONLY" in warning and "does not block" in warning for warning in warnings)


def test_agentcore_missing_entitlement_output_never_infers_from_profile():
    endpoints = {
        "agentcore_deployment_mode": "production",
        "agentcore_production_readiness": "PRODUCTION_OIDC_STATIC_GATES_SATISFIED",
    }
    profiles = [
        _profile(web_search_enabled=True, auth_type="oidc"),
        _profile(
            web_search_enabled=True,
            auth_type="oidc",
            websearch_entitled_groups=["developers"],
            websearch_policy_mode="ENFORCE",
        ),
        _profile(web_search_enabled=True, auth_type="idc"),
        _profile(web_search_enabled=True, auth_type="none"),
    ]

    for profile in profiles:
        warnings = _gateway_readiness_warnings(profile, endpoints)
        assert any("entitlement/authorization posture is unknown/unverified" in warning for warning in warnings)
        assert not any("any valid OIDC token" in warning for warning in warnings)
        assert not any("uses AWS_IAM" in warning for warning in warnings)
        assert not any("no authenticated caller identity" in warning for warning in warnings)


def test_status_reports_only_explicit_legacy_gateway_and_agentcore_stacks():
    profile = _profile(gateway_enabled=True, web_search_enabled=True, websearch_region="us-east-1")
    command = StatusCommand()

    with patch.object(command, "_check_stack", return_value={"status": "CREATE_COMPLETE"}) as check_stack:
        stacks = command._get_stack_status(profile)

    assert set(stacks) == {"gateway (legacy GIP stack)", "websearch gateway"}
    assert check_stack.call_args_list[0].args == ("gip-gateway", "us-west-2")
    assert check_stack.call_args_list[1].args == ("gip-websearch", "us-east-1")


def test_status_does_not_guess_upstream_stack_name_or_region():
    profile = _profile()

    def outputs(stack_name, region):
        assert stack_name != "ClaudeGatewayStack"
        return {}

    with patch("governed_inference_platform.cli.commands.status.get_stack_outputs", side_effect=outputs):
        endpoints = StatusCommand()._get_endpoints(profile)

    assert endpoints == {}


def test_json_status_adds_warnings_without_changing_success_exit_code():
    profile = _profile()
    command = StatusCommand()
    endpoints = {"gateway_source": "aws-samples-upstream"}
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=1000)

    with (
        patch.object(command, "_get_endpoints", return_value=endpoints),
        patch.object(command, "_get_stack_status", return_value={}),
        patch("governed_inference_platform.cli.commands.status.get_configuration_dict", return_value={}),
    ):
        exit_code = command._show_json_status(profile, console)

    payload = json.loads(output.getvalue())
    assert exit_code == 0
    assert len(payload["warnings"]) == 1
    assert "pinned AWS Samples implementation" in payload["warnings"][0]
