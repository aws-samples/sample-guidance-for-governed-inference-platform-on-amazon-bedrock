# ABOUTME: Tests for CoWork 3P credential helper mode (inferenceCredentialHelper)
# ABOUTME: Verifies MDM config generation for both "helper" and "profile" modes

"""Tests for CoWork 3P credential helper mode."""

import copy
import hashlib
import json
import re
from pathlib import Path

import yaml

from governed_inference_platform.cli.utils.cowork_3p import (
    build_mdm_config,
    generate_intune_script,
    generate_json,
    generate_reg_file,
)

COLLECTOR_TEMPLATE = Path(__file__).resolve().parents[2] / "deployment" / "infrastructure" / "otel-collector.yaml"

DELETE_MATCHING_KEYS = re.compile(r'^delete_matching_keys\(([^,]+), "(.*)"\)(?: where .*)?$')
KEEP_KEYS = re.compile(r"^keep_keys\(([^,]+), (\[[^]]*\])\)(?: where .*)?$")
FLATTEN = re.compile(r"^flatten\(([^)]+)\)(?: where .*)?$")
IDENTITY_KEY = re.compile(
    r"(?i)(^|[._-])(user(name)?|enduser|email|principal|session|quota|team|department|organization|manager|login|"
    r"sub(ject)?|uid|actor|cost[._-]?center|tag)([._-]|$)|^(role|location)$"
)


def _collector_configs() -> list[dict]:
    template = COLLECTOR_TEMPLATE.read_text(encoding="utf-8")
    blocks = template.split("        - !Sub |\n")[1:]
    blocks[-1] = blocks[-1].split("\n  # ECS Task Definition", 1)[0]
    configs = []
    for block in blocks:
        config_text = "".join(line.removeprefix("            ") for line in block.splitlines(keepends=True))
        configs.append(yaml.safe_load(config_text))
    return configs


def _target_map(event: dict, target: str) -> dict | None:
    if target == "resource.attributes":
        return event["resource"]["attributes"]
    if target == "log.attributes":
        return event["log"]["attributes"]
    if target == "log.body":
        body = event["log"]["body"]
        return body if isinstance(body, dict) else None
    if target == 'log.body["attributes"]':
        body = event["log"]["body"]
        attributes = body.get("attributes") if isinstance(body, dict) else None
        return attributes if isinstance(attributes, dict) else None
    if target == "scope.attributes":
        return event["scope"]["attributes"]
    return None


def _flatten_map(target: dict) -> None:
    flattened = {}

    def visit(prefix: str, value) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                visit(f"{prefix}.{key}" if prefix else key, nested)
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                visit(f"{prefix}.{index}", nested)
        else:
            flattened[prefix] = value

    visit("", target)
    target.clear()
    target.update(flattened)


def _apply_configured_log_scrub(config: dict, processor_name: str, event: dict) -> dict:
    transformed = copy.deepcopy(event)
    groups = config["processors"][processor_name]["log_statements"]
    for group in groups:
        for statement in group["statements"]:
            match = FLATTEN.match(statement)
            if match:
                target = _target_map(transformed, match.group(1))
                if target is not None:
                    _flatten_map(target)
                continue

            match = DELETE_MATCHING_KEYS.match(statement)
            if match:
                target = _target_map(transformed, match.group(1))
                if target is None:
                    continue
                key_pattern = re.compile(match.group(2))
                for key in list(target):
                    if key_pattern.search(key):
                        del target[key]
                continue

            match = KEEP_KEYS.match(statement)
            if match:
                target = _target_map(transformed, match.group(1))
                if target is None:
                    continue
                allowed = set(json.loads(match.group(2)))
                for key in list(target):
                    if key not in allowed:
                        del target[key]
    return transformed


def _identity_keys(value) -> list[str]:
    found = []
    if isinstance(value, dict):
        for key, nested in value.items():
            if IDENTITY_KEY.search(key):
                found.append(key)
            found.extend(_identity_keys(nested))
    elif isinstance(value, list):
        for nested in value:
            found.extend(_identity_keys(nested))
    return found


def _verified_projection(config: dict, claims: dict) -> dict[str, str]:
    groups = config["processors"]["transform/verified"]["metric_statements"]
    statements = [statement for group in groups for statement in group["statements"]]
    hash_statement = next(statement for statement in statements if 'cache["h"]' in statement)
    id_statement = next(statement for statement in statements if 'attributes["user.id"]' in statement)
    email_statement = next(statement for statement in statements if 'attributes["user.email"]' in statement)

    assert hash_statement == (
        'set(resource.cache["h"], SHA256(Coalesce([resource.cache["c"]["sub"], resource.cache["c"]["user_id"]])))'
    )
    assert id_statement == (
        'set(resource.attributes["user.id"], Concat([Substring(resource.cache["h"], 0, 8), '
        'Substring(resource.cache["h"], 8, 4), Substring(resource.cache["h"], 12, 4), '
        'Substring(resource.cache["h"], 16, 4), Substring(resource.cache["h"], 20, 12)], "-"))'
    )
    assert email_statement == (
        'set(resource.attributes["user.email"], Coalesce([resource.cache["c"]["email"], '
        'resource.cache["c"]["preferred_username"], resource.cache["c"]["mail"], '
        '"unknown@example.com"]))'
    )

    subject = claims.get("sub") or claims.get("user_id")
    digest = hashlib.sha256(subject.encode()).hexdigest()
    user_id = f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"
    email = claims.get("email") or claims.get("preferred_username") or claims.get("mail") or "unknown@example.com"
    return {"user.id": user_id, "user.email": email}


class TestBuildMdmConfigCredentialHelper:
    """Test build_mdm_config with credential_mode='helper' (default)."""

    def test_helper_mode_includes_credential_helper_keys(self):
        """Default mode should produce inferenceCredentialHelper keys."""
        config = build_mdm_config(
            bedrock_region="us-west-2",
            model_aliases=["opus", "sonnet", "haiku"],
            profile_name="MyProfile",
        )
        assert "inferenceCredentialHelper" in config
        assert "inferenceCredentialHelperTtlSec" in config
        assert "inferenceCredentialHelperSilentRefreshEnabled" in config
        assert config["inferenceCredentialHelperSilentRefreshEnabled"] == "true"

    def test_helper_mode_path_includes_profile_name(self):
        """Credential helper path should include --profile <name>."""
        config = build_mdm_config(
            bedrock_region="us-west-2",
            model_aliases=["sonnet"],
            profile_name="Production",
        )
        assert "--profile Production" in config["inferenceCredentialHelper"]
        assert "credential-process" in config["inferenceCredentialHelper"]

    def test_helper_mode_ttl_default(self):
        """Default TTL should be 3500s (under 1h STS expiry)."""
        config = build_mdm_config(
            bedrock_region="us-west-2",
            model_aliases=["sonnet"],
        )
        assert config["inferenceCredentialHelperTtlSec"] == "3500"

    def test_helper_mode_custom_ttl(self):
        """Custom TTL should override default."""
        config = build_mdm_config(
            bedrock_region="us-west-2",
            model_aliases=["sonnet"],
            credential_helper_ttl_sec=1800,
        )
        assert config["inferenceCredentialHelperTtlSec"] == "1800"

    def test_helper_mode_still_includes_bedrock_profile(self):
        """Helper mode should still include inferenceBedrockProfile for SDK fallback."""
        config = build_mdm_config(
            bedrock_region="us-west-2",
            model_aliases=["sonnet"],
            profile_name="gip",
        )
        assert config["inferenceBedrockProfile"] == "gip"

    def test_helper_mode_uses_unix_path_by_default(self):
        """Default macOS path uses the __GIP_HOME__ placeholder (install.sh resolves it)."""
        config = build_mdm_config(
            bedrock_region="us-west-2",
            model_aliases=["sonnet"],
            profile_name="Test",
        )
        assert config["inferenceCredentialHelper"].startswith("__GIP_HOME__/")


class TestBuildMdmConfigProfileMode:
    """Test build_mdm_config with credential_mode='profile' (legacy)."""

    def test_profile_mode_uses_bedrock_profile_only(self):
        """Legacy mode should use inferenceBedrockProfile without credential helper."""
        config = build_mdm_config(
            bedrock_region="us-west-2",
            model_aliases=["opus"],
            profile_name="LegacyProfile",
            credential_mode="profile",
        )
        assert config["inferenceBedrockProfile"] == "LegacyProfile"
        assert "inferenceCredentialHelper" not in config
        assert "inferenceCredentialHelperTtlSec" not in config
        assert "inferenceCredentialHelperSilentRefreshEnabled" not in config

    def test_profile_mode_backward_compatible(self):
        """Legacy mode output should match previous behavior exactly."""
        config = build_mdm_config(
            bedrock_region="eu-west-1",
            model_aliases=["opus", "sonnet", "haiku"],
            profile_name="gip",
            credential_mode="profile",
        )
        assert config == {
            "inferenceProvider": "bedrock",
            "inferenceBedrockRegion": "eu-west-1",
            "inferenceBedrockProfile": "gip",
            "inferenceModels": ["opus", "sonnet", "haiku"],
            "isClaudeCodeForDesktopEnabled": True,
            "isDesktopExtensionEnabled": True,
            "isDesktopExtensionDirectoryEnabled": True,
            "isDesktopExtensionSignatureRequired": True,
            "isLocalDevMcpEnabled": True,
        }


class TestGenerateRegFileCredentialHelper:
    """Test Windows .reg generation with credential helper path rewriting."""

    def test_reg_file_rewrites_unix_path_to_windows(self, tmp_path):
        """Unix path becomes __GIP_HOME__\\...credential-process.exe (placeholder kept).

        The placeholder is NOT %USERPROFILE%: Claude Desktop reads the registry
        value literally and does not expand env vars. install.bat substitutes
        __GIP_HOME__ with the absolute home before importing the .reg.
        """
        config = build_mdm_config(
            bedrock_region="us-west-2",
            model_aliases=["sonnet"],
            profile_name="Test",
        )
        reg_path = generate_reg_file(tmp_path, config)
        content = reg_path.read_text(encoding="utf-8")
        # Placeholder kept, env var NOT used
        assert "__GIP_HOME__" in content
        assert "%USERPROFILE%" not in content
        # Windows binary form: backslashes + .exe suffix
        assert "credential-process.exe" in content
        assert "--profile Test" in content

    def test_reg_file_no_rewrite_for_profile_mode(self, tmp_path):
        """Profile mode should not contain credential helper in .reg file."""
        config = build_mdm_config(
            bedrock_region="us-west-2",
            model_aliases=["sonnet"],
            credential_mode="profile",
        )
        reg_path = generate_reg_file(tmp_path, config)
        content = reg_path.read_text(encoding="utf-8")
        assert "inferenceCredentialHelper" not in content


class TestGenerateIntuneScript:
    """Test Intune .ps1 generation resolves the home placeholder at deploy time."""

    def test_ps1_resolves_home_placeholder_at_runtime(self, tmp_path):
        """The .ps1 resolves __GIP_HOME__ to $env:USERPROFILE when it runs.

        Claude Desktop does not expand env vars in registry MDM values, so the
        script must write an absolute path. It resolves the placeholder at deploy
        time rather than emitting a literal %USERPROFILE%.
        """
        config = build_mdm_config(
            bedrock_region="us-west-2",
            model_aliases=["sonnet"],
            profile_name="Test",
        )
        ps1_path = generate_intune_script(tmp_path, config)
        content = ps1_path.read_text(encoding="utf-8")
        assert "$gipHome = $env:USERPROFILE" in content
        assert ".Replace('__GIP_HOME__', $gipHome)" in content
        # credential helper converted to the Windows .exe form
        assert "credential-process.exe" in content
        assert "--profile Test" in content
        # no emitted registry VALUE carries the unexpanded env var (comments may
        # mention it, so check the Set-ItemProperty lines specifically)
        value_lines = [ln for ln in content.splitlines() if ln.startswith("Set-ItemProperty")]
        assert value_lines  # sanity: we did emit values
        assert all("%USERPROFILE%" not in ln for ln in value_lines)


class TestGenerateJsonCredentialHelper:
    """Test JSON output includes credential helper keys."""

    def test_json_output_includes_helper_keys(self, tmp_path):
        """JSON output should include all credential helper keys."""
        config = build_mdm_config(
            bedrock_region="us-west-2",
            model_aliases=["opus", "sonnet"],
            profile_name="MyProfile",
        )
        json_path = generate_json(tmp_path, config)
        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert data["inferenceCredentialHelper"] == "__GIP_HOME__/gip/credential-process --desktop --profile MyProfile"
        assert data["inferenceCredentialHelperTtlSec"] == "3500"
        assert data["inferenceCredentialHelperSilentRefreshEnabled"] == "true"


class TestCollectorIdentityContainment:
    """Regression coverage for the collector's trusted and aggregate ingress split."""

    def test_caller_identity_headers_are_never_authoritative(self):
        template = COLLECTOR_TEMPLATE.read_text(encoding="utf-8")

        assert "from_context: metadata.x-user" not in template
        assert 'otelcol.client.metadata["authorization"]' in template
        assert "transform/verified" in template

    def test_shared_token_routes_to_aggregate_receiver(self):
        template = COLLECTOR_TEMPLATE.read_text(encoding="utf-8")
        cowork_rule = template.split("  CoWorkListenerRule:", 1)[1].split("  AggregateTargetRegistrationRule:", 1)[0]
        https_listener = template.split("  HTTPSListener:", 1)[1].split("  CoWorkListenerRule:", 1)[0]

        assert "TargetGroupArn: !Ref HTTPTargetGroup" in cowork_rule
        assert "TargetGroupArn: !Ref VerifiedTargetGroup" in https_listener
        assert "endpoint: 0.0.0.0:4318" in template
        assert "endpoint: 0.0.0.0:4319" in template

    def test_verified_receiver_requires_complete_https_oidc(self):
        template = COLLECTOR_TEMPLATE.read_text(encoding="utf-8")
        condition = template.split("  HasVerifiedJwtIngress:", 1)[1].split("  HasCoWorkToken:", 1)[0]
        listener = template.split("  HTTPSListener:", 1)[1].split("  CoWorkListenerRule:", 1)[0]

        for prerequisite in ("EnableHttps", "HasJwtAuth", "HasOidcJwksEndpoint", "HasOidcClientId"):
            assert f"!Condition {prerequisite}" in condition
        assert "- HasVerifiedJwtIngress" in listener
        assert "AdditionalClaims: !If" not in listener

    def test_verified_endpoint_output_exists_only_with_verified_ingress(self):
        template = COLLECTOR_TEMPLATE.read_text(encoding="utf-8")
        output = template.split("  VerifiedCollectorEndpoint:", 1)[1].split("  ALBEndpoint:", 1)[0]

        assert "Condition: HasVerifiedJwtIngress" in output
        assert "https://${CustomDomainName}" in output

    def test_custom_domain_cannot_silently_create_no_collector_service(self):
        template = COLLECTOR_TEMPLATE.read_text(encoding="utf-8")
        rule = template.split("  CustomDomainRequiresHttpsConfiguration:", 1)[1].split(
            "  InternetFacingRequiresHttpsAndJwt:", 1
        )[0]

        assert "RuleCondition: !Not [!Equals [!Ref CustomDomainName, '']]" in rule
        assert "!Ref HostedZoneId" in rule
        assert "!Ref CertificateArn" in rule

    def test_verified_receiver_is_reachable_only_from_alb_security_group(self):
        template = COLLECTOR_TEMPLATE.read_text(encoding="utf-8")
        task_security_group = template.split("  TaskSecurityGroup:", 1)[1].split("  # ACM Certificate", 1)[0]

        assert "FromPort: 4317" in task_security_group
        assert "ToPort: 4319" in task_security_group
        assert "SourceSecurityGroupId: !Ref ALBSecurityGroup" in task_security_group
        assert "CidrIp:" not in task_security_group

    def test_shared_token_rejects_alb_wildcards_and_short_values(self):
        template = COLLECTOR_TEMPLATE.read_text(encoding="utf-8")
        parameter = template.split("  CoWorkServiceToken:", 1)[1].split("  ALBScheme:", 1)[0]

        pattern_match = re.search(r"AllowedPattern: '([^']+)'", parameter)
        assert pattern_match is not None
        pattern = pattern_match.group(1)
        assert re.fullmatch(pattern, "")
        assert re.fullmatch(pattern, "00000000-0000-4000-8000-000000000000")
        for unsafe in ("short", "*" * 32, "?" * 32, "token with spaces that is long enough"):
            assert re.fullmatch(pattern, unsafe) is None

    def test_aggregate_and_verified_scrub_nested_identity_behaviorally(self):
        fixture = {
            "scope": {
                "attributes": {
                    "user.email": "scope-spoof@example.com",
                    "session.id": "scope-session",
                    "library.name": "safe-instrumentation",
                }
            },
            "resource": {
                "attributes": {
                    "service.name": "cowork",
                    "user.email": "resource-spoof@example.com",
                    "principal_arn": "arn:spoofed",
                    "username": "spoofed-login",
                }
            },
            "log": {
                "attributes": {
                    "model": "claude-sonnet",
                    "decision": {"user": {"email": "nested-log-spoof@example.com"}},
                    "user_id": "top-level-spoof",
                    "sub": "spoofed-subject",
                    "quota.remaining": 1,
                },
                "body": {
                    "name": "lam_session_turn_completed",
                    "user.email": "body-spoof@example.com",
                    "context": {"user": {"email": "deep-spoof@example.com"}},
                    "attributes": {
                        "model": "claude-sonnet",
                        "input_tokens": 42,
                        "total_cost_usd": 0.01,
                        "user_email": "nested-spoof@example.com",
                        "session.id": "nested-session",
                        "actor": "nested-actor",
                        "cost_center": "secret-cost-center",
                        "metadata": {"session": {"id": "deep-session"}},
                        "output_tokens": [{"user": {"email": "array-spoof@example.com"}}],
                    },
                },
            },
        }

        for config in _collector_configs():
            for processor_name in ("transform/aggregate", "transform/verified"):
                transformed = _apply_configured_log_scrub(config, processor_name, fixture)

                assert _identity_keys(transformed) == []
                assert transformed["resource"]["attributes"] == {}
                assert transformed["scope"]["attributes"] == {}
                assert transformed["log"]["attributes"]["model"] == "claude-sonnet"
                assert transformed["log"]["body"]["name"] == "lam_session_turn_completed"
                assert transformed["log"]["body"]["attributes"] == {
                    "model": "claude-sonnet",
                    "input_tokens": 42,
                    "total_cost_usd": 0.01,
                }

    def test_projection_is_fail_closed_and_fixes_instrumentation_scope(self):
        for config in _collector_configs():
            for processor_name in ("transform/aggregate", "transform/verified"):
                processor = config["processors"][processor_name]
                assert processor["error_mode"] == "propagate"
                for signal in ("metric_statements", "log_statements"):
                    groups = {group["context"]: group["statements"] for group in processor[signal]}
                    assert 'delete_matching_keys(resource.attributes, ".*")' in groups["resource"]
                    assert 'delete_matching_keys(scope.attributes, ".*")' in groups["scope"]
                    assert 'set(scope.name, "gip")' in groups["scope"]
                    assert 'set(scope.version, "")' in groups["scope"]
                log_statements = {group["context"]: group["statements"] for group in processor["log_statements"]}
                assert "flatten(log.attributes)" in log_statements["log"]
                assert (
                    'flatten(log.body["attributes"]) where IsMap(log.body) and IsMap(log.body["attributes"])'
                    in log_statements["log"]
                )
                assert any("not IsMap(log.body)" in statement for statement in log_statements["log"])

    def test_resource_identity_is_restored_after_scrub_for_dashboard_queries(self):
        for config in _collector_configs():
            attributes = {item["key"]: item["value"] for item in config["processors"]["resource"]["attributes"]}
            assert attributes["service.name"] == "claude-code"
            for pipeline_name, pipeline in config["service"]["pipelines"].items():
                assert pipeline["processors"][0].startswith("transform/")
                assert pipeline["processors"][1] == "resource", pipeline_name

        dashboard = (COLLECTOR_TEMPLATE.parent / "claude-code-dashboard.yaml").read_text(encoding="utf-8")
        assert '\\"@resource.service.name\\"=\\"claude-code\\"' in dashboard

    def test_verified_projection_hashes_subject_and_uses_claim_fallbacks(self):
        fixtures = [
            ({"sub": "opaque-subject", "email": "verified@example.com"}, "verified@example.com"),
            ({"sub": "opaque-subject", "preferred_username": "verified-user"}, "verified-user"),
            ({"user_id": "legacy-user-id", "mail": "mail@example.com"}, "mail@example.com"),
            ({"sub": "opaque-subject"}, "unknown@example.com"),
        ]

        for config in _collector_configs():
            for claims, expected_email in fixtures:
                projected = _verified_projection(config, claims)
                raw_subject = claims.get("sub") or claims["user_id"]
                digest = hashlib.sha256(raw_subject.encode()).hexdigest()
                expected_user_id = f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"

                assert projected["user.id"] == expected_user_id
                assert re.fullmatch(
                    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", projected["user.id"]
                )
                assert projected["user.email"] == expected_email
                assert raw_subject not in json.dumps(projected)

    def test_collector_image_is_pinned_and_reads_environment_config(self):
        template = COLLECTOR_TEMPLATE.read_text(encoding="utf-8")

        # Pinned by manifest-list digest (Docker Hub `otel/opentelemetry-collector-contrib:0.156.0`,
        # resolved 2026-09-02); the human-readable version stays in the parameter Description.
        assert (
            "Default: otel/opentelemetry-collector-contrib"
            "@sha256:125bdbeb7590cc1952c5b3430ecf14063568980c2c93d5b38676cc0446ed8108\n"
        ) in template
        assert "OpenTelemetry Collector Contrib 0.156.0, pinned by digest" in template
        assert "--config=env:AOT_CONFIG_CONTENT" in template
        assert len(template.encode("utf-8")) <= 51_200

        config_blocks = template.split("        - !Sub |\n")[1:]
        config_blocks[-1] = config_blocks[-1].split("\n  # ECS Task Definition", 1)[0]
        for block in config_blocks:
            config = "".join(line.removeprefix("            ") for line in block.splitlines(keepends=True))
            assert len(config.encode("utf-8")) <= 8_192
