"""Regression tests for infrastructure template contracts."""

import re
from pathlib import Path

import pytest
import yaml

from tests.test_cloudformation import CloudFormationLoader

INFRA_DIR = Path(__file__).parent.parent.parent / "deployment" / "infrastructure"

AUTH_TEMPLATES = [
    "bedrock-auth-okta.yaml",
    "bedrock-auth-azure.yaml",
    "bedrock-auth-auth0.yaml",
    "bedrock-auth-google.yaml",
    "bedrock-auth-generic.yaml",
    "bedrock-auth-cognito-pool.yaml",
    "bedrock-auth-idc.yaml",
    "cognito-identity-pool.yaml",
]

TAG_TEMPLATES = ["quota-metering.yaml", "memory-stack.yaml", "model-lifecycle.yaml"]


def _load(name: str) -> dict:
    with (INFRA_DIR / name).open(encoding="utf-8") as template_file:
        return yaml.load(template_file, Loader=CloudFormationLoader)  # nosec B506


def _literal_tag_values(node):
    if isinstance(node, dict):
        tags = node.get("Tags")
        if isinstance(tags, list):
            for tag in tags:
                value = tag.get("Value")
                if isinstance(value, str):
                    yield value
        elif isinstance(tags, dict):
            for value in tags.values():
                if isinstance(value, str):
                    yield value
        for value in node.values():
            yield from _literal_tag_values(value)
    elif isinstance(node, list):
        for value in node:
            yield from _literal_tag_values(value)


@pytest.mark.parametrize("template_name", TAG_TEMPLATES)
def test_service_tag_values_exclude_parentheses_and_commas(template_name):
    values = list(_literal_tag_values(_load(template_name)))
    assert values, f"{template_name} did not expose any literal tag values"
    invalid = [value for value in values if any(character in value for character in "(),")]
    assert invalid == [], f"{template_name} has service-incompatible tag values: {invalid}"


@pytest.mark.parametrize("template_name", AUTH_TEMPLATES)
def test_auth_templates_allow_apply_guardrail_only_for_own_account(template_name):
    statements = _load(template_name)["Resources"]["BedrockAccessPolicy"]["Properties"]["PolicyDocument"]["Statement"]
    by_sid = {statement.get("Sid"): statement for statement in statements if isinstance(statement, dict)}
    grant = by_sid["AllowOwnAccountGuardrails"]

    assert grant["Effect"] == "Allow"
    assert grant["Action"] == "bedrock:ApplyGuardrail"
    assert grant["Resource"] == {"Fn::Sub": "arn:${AWS::Partition}:bedrock:*:${AWS::AccountId}:guardrail/*"}
    assert grant["Condition"] == {"StringEquals": {"aws:RequestedRegion": {"Ref": "AllowedBedrockRegions"}}}


@pytest.mark.parametrize("template_name", AUTH_TEMPLATES)
def test_guardrail_grant_does_not_change_anthropic_model_baseline(template_name):
    template = _load(template_name)
    parameter = template["Parameters"]["RestrictToAnthropicModels"]
    strings = str(template["Resources"]["BedrockAccessPolicy"])

    assert parameter["Default"] == "true"
    assert "foundation-model/anthropic.*" in strings


def test_gateway_policy_engine_permissions_are_conditional_and_scoped():
    statements = _load("bedrock-agentcore-gateway.yaml")["Resources"]["GatewayExecutionRole"]["Properties"]["Policies"][
        0
    ]["PolicyDocument"]["Statement"]
    conditionals = [statement["Fn::If"] for statement in statements if "Fn::If" in statement]
    by_sid = {entry[1]["Sid"]: entry for entry in conditionals}

    engine = by_sid["GetPolicyEngine"]
    assert engine[0] == "HasEntitlement"
    assert engine[1]["Action"] == [
        "bedrock-agentcore:GetPolicyEngine",
        "bedrock-agentcore:AuthorizeAction",
        "bedrock-agentcore:PartiallyAuthorizeActions",
    ]
    assert engine[1]["Resource"] == {"Ref": "GatewayPolicyEngine"}

    gateway = by_sid["AuthorizeGatewayAction"]
    assert gateway[0] == "HasEntitlement"
    assert gateway[1]["Action"] == [
        "bedrock-agentcore:AuthorizeAction",
        "bedrock-agentcore:PartiallyAuthorizeActions",
    ]
    assert gateway[1]["Resource"] == {
        "Fn::Sub": (
            "arn:${AWS::Partition}:bedrock-agentcore:${AWS::Region}:${AWS::AccountId}:gateway/${AWS::StackName}-gw-*"
        )
    }


def test_gateway_rejects_group_entitlements_and_enforcement_before_create():
    template = _load("bedrock-agentcore-gateway.yaml")
    groups = template["Parameters"]["EntitledGroups"]
    policy_mode = template["Parameters"]["PolicyMode"]
    statement = template["Resources"]["GatewayEntitlementPolicy"]["Properties"]["Definition"]["Cedar"]["Statement"]

    assert groups["Type"] == "String"
    assert groups["Default"] == ""
    assert groups["AllowedPattern"] == "^$"
    assert policy_mode["AllowedValues"] == ["LOG_ONLY"]
    assert statement.strip() == "forbid(principal, action, resource);"
    assert "containsAny" not in statement
    assert "like" not in statement


# ---------------------------------------------------------------------------
# security review: every SNS topic the guidance creates is CMK-encrypted
# ---------------------------------------------------------------------------

SNS_TOPIC_TEMPLATES = ["quota-monitoring.yaml", "model-lifecycle.yaml", "skills-registry.yaml"]


def test_sns_topics_live_only_in_the_three_encrypted_templates():
    """Keeps the parametrized invariant below honest: a topic added elsewhere
    must join this list (and get a key) rather than slip past the check."""
    with_topics = sorted(
        path.name
        for path in INFRA_DIR.glob("*.yaml")
        if re.search(r"^\s+Type:\s+['\"]?AWS::SNS::Topic['\"]?\s*$", path.read_text(encoding="utf-8"), re.MULTILINE)
    )
    assert with_topics == sorted(SNS_TOPIC_TEMPLATES)


@pytest.mark.parametrize("template_name", SNS_TOPIC_TEMPLATES)
def test_every_sns_topic_is_encrypted_with_a_rotating_stack_owned_key(template_name):
    resources = _load(template_name)["Resources"]
    topics = {name: r for name, r in resources.items() if r["Type"] == "AWS::SNS::Topic"}
    assert topics, f"{template_name} has no SNS topic"
    for name, topic in topics.items():
        key_ref = topic["Properties"].get("KmsMasterKeyId")
        assert key_ref, f"{template_name}: {name} is not encrypted (no KmsMasterKeyId)"
        key = resources[key_ref["Ref"]]
        assert key["Type"] == "AWS::KMS::Key", f"{template_name}: {name} must use a stack-owned CMK, not aws/sns"
        assert topic.get("Condition") == "CreateTopic"
        # Lifecycle/quota keys exist only with their topic. The skills key is
        # shared with an unconditional EventBridge-written DLQ, so it must be
        # unconditional too.
        if key.get("Condition") is None:
            queues = {n: r for n, r in resources.items() if r["Type"] == "AWS::SQS::Queue"}
            assert any(q["Properties"].get("KmsMasterKeyId") == key_ref for q in queues.values())
        else:
            assert key["Condition"] == "CreateTopic"
        assert key["Properties"]["EnableKeyRotation"] is True
        assert key["Properties"]["PendingWindowInDays"] == 7
        statements = key["Properties"]["KeyPolicy"]["Statement"]
        assert statements[0]["Principal"] == {
            "AWS": {"Fn::Sub": "arn:${AWS::Partition}:iam::${AWS::AccountId}:root"}
        }, "account-root admin statement must come first"
        # No wildcard principal anywhere in the key policy (checkov CKV_AWS_33).
        assert all(s["Principal"] != "*" and s["Principal"] not in ({"AWS": "*"},) for s in statements)
        # Scanner metadata must not re-suppress the encryption checks.
        metadata = str(topic.get("Metadata", {}))
        assert "CKV_AWS_26" not in metadata and "SNS_ENCRYPTED_KMS" not in metadata, f"{template_name}: {name}"


@pytest.mark.parametrize("template_name", SNS_TOPIC_TEMPLATES)
def test_sns_encryption_suppressions_absent(template_name):
    raw = (INFRA_DIR / template_name).read_text(encoding="utf-8")
    for marker in ("CKV_AWS_26", "SNS_ENCRYPTED_KMS"):
        assert marker not in raw, f"{template_name} still suppresses {marker}"


def test_skills_registry_uses_one_shared_key_for_topic_and_eventbridge_dlq():
    resources = _load("skills-registry.yaml")["Resources"]
    keys = {name: resource for name, resource in resources.items() if resource["Type"] == "AWS::KMS::Key"}
    assert set(keys) == {"CuratorTopicKey"}

    key = keys["CuratorTopicKey"]
    assert "Condition" not in key  # the DLQ exists even when an external topic is supplied
    assert key["Properties"]["EnableKeyRotation"] is True
    assert key["Properties"]["PendingWindowInDays"] == 7
    assert resources["CuratorTopicKeyAlias"]["Properties"]["TargetKeyId"] == {"Ref": "CuratorTopicKey"}
    assert resources["CuratorTopic"]["Properties"]["KmsMasterKeyId"] == {"Ref": "CuratorTopicKey"}
    assert resources["DistributorDeadLetterQueue"]["Properties"]["KmsMasterKeyId"] == {"Ref": "CuratorTopicKey"}

    statements = {statement["Sid"]: statement for statement in key["Properties"]["KeyPolicy"]["Statement"]}
    assert set(statements) == {"AccountAdmin", "SnsServiceUse", "EventBridgePublish"}
    assert statements["SnsServiceUse"]["Condition"]["StringEquals"]["kms:EncryptionContext:aws:sns:topicArn"] == {
        "Fn::Sub": "arn:${AWS::Partition}:sns:${AWS::Region}:${AWS::AccountId}:${AWS::StackName}-pending-approval"
    }
    assert statements["EventBridgePublish"]["Principal"] == {"Service": "events.amazonaws.com"}
    assert "Condition" not in statements["EventBridgePublish"]

    role_statements = [
        statement
        for policy in resources["DistributorRole"]["Properties"]["Policies"]
        for statement in policy["PolicyDocument"]["Statement"]
        if isinstance(statement, dict)
    ]
    key_use = next(statement for statement in role_statements if statement.get("Sid") == "UseDeadLetterQueueKey")
    assert set(key_use["Action"]) == {"kms:GenerateDataKey", "kms:Decrypt"}
    assert key_use["Resource"] == {"Fn::GetAtt": ["CuratorTopicKey", "Arn"]}


# ---------------------------------------------------------------------------
# Scanner contract (security scanners list suppressed Checkov findings, so the
# posture is pinned by content, not by skips)
# ---------------------------------------------------------------------------

ALL_TEMPLATES = sorted(p.name for p in INFRA_DIR.glob("*.yaml"))


def _resources_of_type(template: dict, type_name: str) -> dict:
    return {k: v for k, v in template.get("Resources", {}).items() if v.get("Type") == type_name}


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_every_queue_and_topic_names_a_kms_key(template_name):
    """CKV_AWS_26/27 and the guard SNS_ENCRYPTED_KMS / SQS_QUEUE_KMS_MASTER_KEY_ID_RULE
    rules read KmsMasterKeyId only; SqsManagedSseEnabled is mutually exclusive."""
    template = _load(template_name)
    for logical_id, queue in _resources_of_type(template, "AWS::SQS::Queue").items():
        assert "KmsMasterKeyId" in queue["Properties"], f"{template_name}/{logical_id}"
        assert "SqsManagedSseEnabled" not in queue["Properties"], f"{template_name}/{logical_id}"
    for logical_id, topic in _resources_of_type(template, "AWS::SNS::Topic").items():
        assert "KmsMasterKeyId" in topic["Properties"], f"{template_name}/{logical_id}"


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_every_customer_managed_key_rotates_and_has_no_wildcard_principal(template_name):
    """CKV_AWS_7 / CKV_AWS_33 and guard CMK_BACKING_KEY_ROTATION_ENABLED /
    KMS_NO_WILDCARD_PRINCIPAL on every AWS::KMS::Key in the repo."""
    template = _load(template_name)
    for logical_id, key in _resources_of_type(template, "AWS::KMS::Key").items():
        assert key["Properties"]["EnableKeyRotation"] is True, f"{template_name}/{logical_id}"
        for statement in key["Properties"]["KeyPolicy"]["Statement"]:
            if statement.get("Effect") == "Deny":
                continue
            principal = statement["Principal"]
            aws = principal.get("AWS") if isinstance(principal, dict) else principal
            assert principal != "*" and aws != "*", f"{template_name}/{logical_id}: wildcard principal"


def test_remaining_checkov_skips_are_the_documented_set():
    """Only findings with a documented AWS rationale
    may remain suppressed: CKV_AWS_18 x8 (Security Hub S3.9), CKV_AWS_67 x1
    (regional Bedrock trail) and CKV_AWS_149 x2 (aws/secretsmanager key)."""
    remaining = []
    for template_name in ALL_TEMPLATES:
        for logical_id, resource in _load(template_name).get("Resources", {}).items():
            for skip in resource.get("Metadata", {}).get("checkov", {}).get("skip", []):
                remaining.append((skip["id"], template_name, logical_id))
    by_id = {}
    for check_id, template_name, logical_id in remaining:
        by_id.setdefault(check_id, []).append(f"{template_name}/{logical_id}")
    assert set(by_id) == {"CKV_AWS_18", "CKV_AWS_67", "CKV_AWS_149"}, by_id
    assert len(by_id["CKV_AWS_18"]) == 8, by_id["CKV_AWS_18"]
    assert by_id["CKV_AWS_67"] == ["cognito-identity-pool.yaml/BedrockCloudTrail"]
    assert sorted(by_id["CKV_AWS_149"]) == [
        "distribution.yaml/DistributionUserSecret",
        "presigned-s3-distribution.yaml/DistributionUserSecret",
    ]
