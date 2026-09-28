from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from rich.console import Console

from governed_inference_platform.cli.commands.deploy import DeployCommand

INFRA = Path(__file__).resolve().parents[2] / "deployment" / "infrastructure"


def test_otel_alb_defaults_internal():
    template = (INFRA / "otel-collector.yaml").read_text(encoding="utf-8")

    assert "ALBScheme:" in template
    assert "Default: 'internal'" in template


def test_cowork_bypass_requires_https_condition():
    template = (INFRA / "otel-collector.yaml").read_text(encoding="utf-8")
    start = template.index("HasCoWorkAuthBypass:")
    end = template.index("AnalyticsEnabled:")
    condition = template[start:end]

    assert "!Condition EnableHttps" in condition


def test_cowork_bypass_is_internal_only():
    template = (INFRA / "otel-collector.yaml").read_text(encoding="utf-8")
    start = template.index("HasCoWorkAuthBypass:")
    end = template.index("AnalyticsEnabled:")
    condition = template[start:end]

    assert "!Condition HasCoWorkToken" in condition
    assert "!Condition IsInternalALB" in condition


def test_internet_facing_monitoring_requires_https_and_jwt_rule():
    template = (INFRA / "otel-collector.yaml").read_text(encoding="utf-8")

    start = template.index("InternetFacingRequiresHttpsAndJwt:")
    end = template.index("Resources:")
    rule = template[start:end]

    assert "ALBScheme" in rule
    assert "CustomDomainName" in rule
    assert "HostedZoneId" in rule
    assert "CertificateArn" in rule
    assert "OidcIssuerUrl" in rule
    assert "OidcJwksEndpoint" in rule
    assert "OidcClientId" in rule


def test_internet_facing_monitoring_rejects_cowork_service_token():
    template = (INFRA / "otel-collector.yaml").read_text(encoding="utf-8")
    start = template.index("InternetFacingRequiresHttpsAndJwt:")
    end = template.index("Resources:")
    rule = template[start:end]

    assert "Assert: !Equals [!Ref CoWorkServiceToken, '']" in rule
    assert "CoWorkServiceToken is forbidden for internet-facing load balancers" in rule


def test_plaintext_collector_ingress_is_explicit_internal_only_opt_in():
    template = (INFRA / "otel-collector.yaml").read_text(encoding="utf-8")

    parameters_section = template.split("\nParameters:", 1)[1]
    parameter = parameters_section.split("\n  AllowInsecureHttpIngress:", 1)[1].split("\n  OidcIssuerUrl:", 1)[0]
    assert "Default: 'false'" in parameter
    assert "AllowedValues: ['true', 'false']" in parameter

    rules = template.split("Rules:", 1)[1].split("Resources:", 1)[0]
    secure_default = rules.split("CollectorIngressRequiresTlsOrExplicitInsecureOptIn:", 1)[1].split(
        "InsecureHttpIngressIsInternalLocalDevelopmentOnly:", 1
    )[0]
    insecure_exception = rules.split("InsecureHttpIngressIsInternalLocalDevelopmentOnly:", 1)[1].split(
        "CustomDomainRequiresHttpsConfiguration:", 1
    )[0]
    assert "CustomDomainName" in secure_default
    assert "AllowInsecureHttpIngress" in secure_default
    assert "ALBScheme" in insecure_exception
    assert "'internal'" in insecure_exception


def test_deploy_guard_blocks_public_monitoring_without_jwt_auth():
    deploy_py = (
        Path(__file__).resolve().parents[1] / "governed_inference_platform" / "cli" / "commands" / "deploy.py"
    ).read_text(encoding="utf-8")

    assert "has_monitoring_https" in deploy_py
    assert "has_monitoring_jwt_auth" in deploy_py
    assert "Internet-facing monitoring requires a supported OIDC provider" in deploy_py


def _monitoring_profile(monitoring_config):
    return SimpleNamespace(  # nosec B106 -- empty placeholder fields, not credentials
        identity_pool_name="test",
        stack_names={"monitoring": "test-otel-collector"},
        monitoring_config=monitoring_config,
        provider_type="",
        provider_domain="",
        client_id="",
        cowork_service_token="",
        analytics_enabled=False,
        aws_region="us-east-1",
        tags=None,
    )


def test_deploy_rejects_implicit_plaintext_collector_ingress():
    command = DeployCommand()
    manager = Mock()
    profile = _monitoring_profile({"create_vpc": False, "vpc_id": "vpc-123", "subnet_ids": ["subnet-a", "subnet-b"]})

    with patch.object(command, "_ensure_ecs_service_linked_role"):
        result = command._deploy_stack("monitoring", profile, Console(), manager)

    assert result == 1
    manager.deploy_stack.assert_not_called()


def test_deploy_wires_explicit_internal_insecure_opt_in():
    command = DeployCommand()
    manager = Mock()
    manager.deploy_stack.return_value = Mock(success=False, error="stop after parameter capture")
    profile = _monitoring_profile(
        {
            "create_vpc": False,
            "vpc_id": "vpc-123",
            "subnet_ids": ["subnet-a", "subnet-b"],
            "alb_scheme": "internal",
            "allow_insecure_http_ingress": True,
        }
    )

    with patch.object(command, "_ensure_ecs_service_linked_role"):
        result = command._deploy_stack("monitoring", profile, Console(), manager)

    assert result == 1
    params = {
        parameter["ParameterKey"]: parameter["ParameterValue"]
        for parameter in manager.deploy_stack.call_args.kwargs["parameters"]
    }
    assert params["AllowInsecureHttpIngress"] == "true"
    assert params["ALBScheme"] == "internal"


def test_deploy_rejects_internet_facing_insecure_opt_in():
    command = DeployCommand()
    manager = Mock()
    profile = _monitoring_profile(
        {
            "create_vpc": False,
            "vpc_id": "vpc-123",
            "subnet_ids": ["subnet-a", "subnet-b"],
            "alb_scheme": "internet-facing",
            "allow_insecure_http_ingress": True,
        }
    )

    with patch.object(command, "_ensure_ecs_service_linked_role"):
        result = command._deploy_stack("monitoring", profile, Console(), manager)

    assert result == 1
    manager.deploy_stack.assert_not_called()
