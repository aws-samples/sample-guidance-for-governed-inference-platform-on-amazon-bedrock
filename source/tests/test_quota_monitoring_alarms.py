# ABOUTME: Contract tests for quota enforcement failure visibility.
# ABOUTME: Ensures fail-closed outages produce operator alarms instead of silent fleet lockout.

from pathlib import Path

import yaml

from tests.test_cloudformation import CloudFormationLoader

TEMPLATE_PATH = Path(__file__).parent.parent.parent / "deployment" / "infrastructure" / "quota-monitoring.yaml"
# gip deploy hands this stack's topic to model-lifecycle as AlertTopicArn,
# so that template's service publishers need grants on this key and topic too.
LIFECYCLE_TEMPLATE_PATH = TEMPLATE_PATH.parent / "model-lifecycle.yaml"

TOPIC_REF = {"Fn::If": ["CreateTopic", {"Ref": "QuotaAlertTopic"}, {"Ref": "AlertTopicArn"}]}
LIFECYCLE_TOPIC_REF = {"Fn::If": ["CreateTopic", {"Ref": "LifecycleAlertTopic"}, {"Ref": "AlertTopicArn"}]}
KMS_PUBLISHER_ACTIONS = {"kms:GenerateDataKey", "kms:Decrypt"}


def _template() -> dict:
    with open(TEMPLATE_PATH, encoding="utf-8") as handle:
        return yaml.load(handle, Loader=CloudFormationLoader)  # nosec B506


def _lifecycle_template() -> dict:
    with open(LIFECYCLE_TEMPLATE_PATH, encoding="utf-8") as handle:
        return yaml.load(handle, Loader=CloudFormationLoader)  # nosec B506


def _service_publishers(resources: dict, topic_ref: dict) -> set[str]:
    """Derive AWS service principals that publish to a topic reference."""
    publishers = set()
    for resource in resources.values():
        props = resource.get("Properties", {})
        if resource["Type"] == "AWS::CloudWatch::Alarm" and topic_ref in props.get("AlarmActions", []):
            publishers.add("cloudwatch.amazonaws.com")
        if resource["Type"] == "AWS::Events::Rule" and any(
            target["Arn"] == topic_ref for target in props.get("Targets", [])
        ):
            publishers.add("events.amazonaws.com")
    return publishers


def _publishing_roles(resources: dict) -> set[str]:
    """Derive execution roles that publish to the quota alert topic."""
    return {
        name
        for name, resource in resources.items()
        if resource["Type"] == "AWS::IAM::Role"
        and any(
            "sns:Publish" in statement["Action"] and statement["Resource"] == TOPIC_REF
            for policy in resource["Properties"]["Policies"]
            for statement in policy["PolicyDocument"]["Statement"]
        )
    }


def _key_statements(resources: dict) -> dict[str, dict]:
    statements = resources["QuotaAlertTopicKey"]["Properties"]["KeyPolicy"]["Statement"]
    return {statement["Sid"]: statement for statement in statements}


def test_quota_stack_alarms_on_enforcement_failures():
    resources = _template()["Resources"]

    for logical_id in ("QuotaCheckErrorsAlarm", "QuotaMonitorErrorsAlarm", "QuotaApi5xxAlarm"):
        alarm = resources[logical_id]
        assert alarm["Type"] == "AWS::CloudWatch::Alarm"
        assert alarm["Properties"]["AlarmActions"] == [TOPIC_REF]
        assert alarm["Properties"]["TreatMissingData"] == "notBreaching"

    assert resources["QuotaCheckErrorsAlarm"]["Properties"]["MetricName"] == "CheckFailures"
    assert resources["QuotaMonitorErrorsAlarm"]["Properties"]["MetricName"] == "MonitorFailures"
    assert resources["QuotaApi5xxAlarm"]["Properties"]["MetricName"] == "5xx"


def test_quota_api_has_stack_local_throttling_limits():
    settings = _template()["Resources"]["QuotaCheckStage"]["Properties"]["DefaultRouteSettings"]

    assert settings["ThrottlingRateLimit"] == 1000
    assert settings["ThrottlingBurstLimit"] == 2000


def test_quota_alert_topic_encrypted_with_stack_key_and_name_unchanged():
    resources = _template()["Resources"]
    topic = resources["QuotaAlertTopic"]
    assert topic["Condition"] == "CreateTopic"
    assert topic["Properties"]["KmsMasterKeyId"] == {"Ref": "QuotaAlertTopicKey"}
    assert topic["Properties"]["TopicName"] == "gip-quota-alerts"
    assert "Metadata" not in topic


def test_quota_alert_topic_key_hygiene():
    resources = _template()["Resources"]
    key = resources["QuotaAlertTopicKey"]
    assert key["Type"] == "AWS::KMS::Key"
    assert key["Condition"] == "CreateTopic"
    assert key["Properties"]["EnableKeyRotation"] is True
    assert key["Properties"]["PendingWindowInDays"] == 7
    assert "gip-quota-alerts" in key["Properties"]["Description"]

    alias = resources["QuotaAlertTopicKeyAlias"]
    assert alias["Type"] == "AWS::KMS::Alias"
    assert alias["Condition"] == "CreateTopic"
    assert alias["Properties"]["AliasName"] == {"Fn::Sub": "alias/${AWS::StackName}-quota-alerts"}
    assert alias["Properties"]["TargetKeyId"] == {"Ref": "QuotaAlertTopicKey"}


def test_quota_alert_key_policy_grants_exactly_the_service_publishers():
    """The key covers this stack and the model-lifecycle stack that reuses its topic."""
    resources = _template()["Resources"]
    statements = _key_statements(resources)
    admin = statements["AccountAdmin"]
    assert admin["Principal"] == {"AWS": {"Fn::Sub": "arn:${AWS::Partition}:iam::${AWS::AccountId}:root"}}
    assert admin["Action"] == "kms:*"

    service_statements = [statement for statement in statements.values() if "Service" in statement.get("Principal", {})]
    granted = {statement["Principal"]["Service"] for statement in service_statements}
    own = _service_publishers(resources, TOPIC_REF)
    reusing = _service_publishers(_lifecycle_template()["Resources"], LIFECYCLE_TOPIC_REF)
    assert own == {"cloudwatch.amazonaws.com"}
    assert reusing == {"events.amazonaws.com", "cloudwatch.amazonaws.com"}
    assert granted == own | reusing | {"sns.amazonaws.com"}

    for statement in service_statements:
        assert statement["Effect"] == "Allow"
        assert statement["Resource"] == "*"
        assert "kms:Decrypt" in statement["Action"]
        assert any(action.startswith("kms:GenerateDataKey") for action in statement["Action"])
        assert not any(action == "kms:*" or action.endswith(":*") for action in statement["Action"])

    cloudwatch = next(
        statement for statement in service_statements if statement["Principal"]["Service"] == "cloudwatch.amazonaws.com"
    )
    assert cloudwatch["Condition"] == {"StringEquals": {"aws:SourceAccount": {"Ref": "AWS::AccountId"}}}

    events = next(
        statement for statement in service_statements if statement["Principal"]["Service"] == "events.amazonaws.com"
    )
    assert "Condition" not in events

    sns = next(
        statement for statement in service_statements if statement["Principal"]["Service"] == "sns.amazonaws.com"
    )
    assert sns["Condition"]["StringEquals"]["kms:EncryptionContext:aws:sns:topicArn"] == {
        "Fn::Sub": "arn:${AWS::Partition}:sns:${AWS::Region}:${AWS::AccountId}:gip-quota-alerts"
    }


def test_quota_alert_topic_policy_grants_all_service_publishers():
    resources = _template()["Resources"]
    policy = resources["QuotaAlertTopicPolicy"]
    assert policy["Type"] == "AWS::SNS::TopicPolicy"
    assert policy["Condition"] == "CreateTopic"
    assert policy["Properties"]["Topics"] == [{"Ref": "QuotaAlertTopic"}]

    own = _service_publishers(resources, TOPIC_REF)
    reusing = _service_publishers(_lifecycle_template()["Resources"], LIFECYCLE_TOPIC_REF)
    expected = own | reusing
    statements = policy["Properties"]["PolicyDocument"]["Statement"]
    by_principal = {statement["Principal"]["Service"]: statement for statement in statements}
    assert expected == {"cloudwatch.amazonaws.com", "events.amazonaws.com"}
    assert set(by_principal) == expected
    for statement in by_principal.values():
        assert statement["Effect"] == "Allow"
        assert statement["Action"] == "sns:Publish"
        assert statement["Resource"] == {"Ref": "QuotaAlertTopic"}
        assert statement["Condition"] == {"StringEquals": {"aws:SourceAccount": {"Ref": "AWS::AccountId"}}}


def test_external_alert_topic_key_parameter_is_optional_and_validated():
    template = _template()
    parameter = template["Parameters"]["AlertTopicKmsKeyArn"]

    assert parameter["Default"] == ""
    assert parameter["AllowedPattern"] == ("^$|^arn:[a-z\\-]+:kms:[a-z0-9\\-]+:[0-9]{12}:key/[a-zA-Z0-9\\-]+$")
    description = parameter["Description"].lower()
    assert all(term in description for term in ("topic policy", "key policy", "cross-account"))

    interface = template["Metadata"]["AWS::CloudFormation::Interface"]
    alerting_group = next(group for group in interface["ParameterGroups"] if group["Label"]["default"] == "Alerting")
    assert alerting_group["Parameters"] == ["AlertTopicArn", "AlertTopicKmsKeyArn"]
    assert interface["ParameterLabels"]["AlertTopicKmsKeyArn"] == {
        "default": "KMS key ARN of the existing alert topic (empty = not encrypted)"
    }


def test_created_topic_path_grants_every_lambda_publisher_exact_key_access():
    template = _template()
    resources = template["Resources"]
    publishers = _publishing_roles(resources)
    assert publishers == {"QuotaMonitorRole", "SidecarMonitorRole"}

    grants = {name: resources[name] for name in ("QuotaMonitorKmsPolicy", "SidecarMonitorKmsPolicy")}
    assert set(grants) == {"QuotaMonitorKmsPolicy", "SidecarMonitorKmsPolicy"}

    granted_roles = set()
    for resource in grants.values():
        (statement,) = resource["Properties"]["PolicyDocument"]["Statement"]
        assert set(statement["Action"]) == KMS_PUBLISHER_ACTIONS
        assert statement["Resource"] == {"Fn::GetAtt": ["QuotaAlertTopicKey", "Arn"]}
        granted_roles.update(role["Ref"] for role in resource["Properties"]["Roles"])
    assert granted_roles == publishers

    assert grants["QuotaMonitorKmsPolicy"]["Condition"] == "CreateTopic"
    assert grants["SidecarMonitorKmsPolicy"]["Condition"] == "BypassDetectionWithCreatedTopic"
    assert template["Conditions"]["BypassDetectionWithCreatedTopic"] == {
        "Fn::And": [{"Condition": "BypassDetectionEnabled"}, {"Condition": "CreateTopic"}]
    }

    for role_name in ("QuotaMonitorRole", "SidecarMonitorRole", "QuotaCheckRole"):
        actions = {
            action
            for policy in resources[role_name]["Properties"]["Policies"]
            for statement in policy["PolicyDocument"]["Statement"]
            for action in statement["Action"]
        }
        assert not any(action.startswith("kms:") for action in actions), role_name


def test_external_encrypted_topic_grants_every_lambda_publisher_exact_key_access():
    template = _template()
    resources = template["Resources"]
    publishers = _publishing_roles(resources)
    grants = {name: resources[name] for name in ("QuotaMonitorExternalKmsPolicy", "SidecarMonitorExternalKmsPolicy")}

    granted_roles = set()
    for resource in grants.values():
        (statement,) = resource["Properties"]["PolicyDocument"]["Statement"]
        assert set(statement["Action"]) == KMS_PUBLISHER_ACTIONS
        assert statement["Resource"] == {"Ref": "AlertTopicKmsKeyArn"}
        assert statement["Resource"] != "*"
        assert not any(action == "kms:*" or action.endswith(":*") for action in statement["Action"])
        granted_roles.update(role["Ref"] for role in resource["Properties"]["Roles"])
    assert granted_roles == publishers

    assert grants["QuotaMonitorExternalKmsPolicy"]["Condition"] == "UsesExternalTopicKey"
    assert grants["SidecarMonitorExternalKmsPolicy"]["Condition"] == "BypassDetectionWithExternalTopicKey"


def test_blank_external_key_creates_no_kms_grant():
    template = _template()
    conditions = template["Conditions"]

    assert conditions["HasAlertTopicKmsKey"] == {"Fn::Not": [{"Fn::Equals": [{"Ref": "AlertTopicKmsKeyArn"}, ""]}]}
    assert conditions["UsesExternalTopicKey"] == {
        "Fn::And": [{"Fn::Not": [{"Condition": "CreateTopic"}]}, {"Condition": "HasAlertTopicKmsKey"}]
    }
    assert conditions["BypassDetectionWithExternalTopicKey"] == {
        "Fn::And": [{"Condition": "BypassDetectionEnabled"}, {"Condition": "UsesExternalTopicKey"}]
    }


def test_effective_quota_alert_key_arn_exported_for_topic_reusers():
    template = _template()
    output = template["Outputs"]["QuotaAlertTopicKmsKeyArn"]
    assert template["Conditions"]["HasEffectiveAlertTopicKmsKey"] == {
        "Fn::Or": [{"Condition": "CreateTopic"}, {"Condition": "UsesExternalTopicKey"}]
    }
    assert output["Condition"] == "HasEffectiveAlertTopicKmsKey"
    assert output["Value"] == {
        "Fn::If": [
            "CreateTopic",
            {"Fn::GetAtt": ["QuotaAlertTopicKey", "Arn"]},
            {"Ref": "AlertTopicKmsKeyArn"},
        ]
    }
    assert output["Export"]["Name"] == {"Fn::Sub": "${AWS::StackName}-QuotaAlertTopicKmsKeyArn"}


def test_dead_letter_queues_use_the_aws_managed_sqs_key():
    resources = _template()["Resources"]
    for logical_id in ("QuotaMonitorDLQ", "SidecarMonitorDLQ"):
        queue = resources[logical_id]
        assert queue["Properties"]["KmsMasterKeyId"] == "alias/aws/sqs", logical_id
        assert "SqsManagedSseEnabled" not in queue["Properties"], logical_id
        assert "Metadata" not in queue, logical_id


def test_quota_tables_name_the_aws_managed_dynamodb_key():
    resources = _template()["Resources"]
    for logical_id in ("QuotaPolicies", "UserQuotaMetrics"):
        table = resources[logical_id]
        assert table["Properties"]["SSESpecification"] == {
            "SSEEnabled": True,
            "SSEType": "KMS",
            "KMSMasterKeyId": "alias/aws/dynamodb",
        }, logical_id
        assert "Metadata" not in table, logical_id


def test_fixed_encryption_findings_have_no_suppressions():
    raw = TEMPLATE_PATH.read_text(encoding="utf-8")
    for marker in (
        "CKV_AWS_26",
        "SNS_ENCRYPTED_KMS",
        "D1 decision memo",
        "CKV_AWS_27",
        "SQS_QUEUE_KMS_MASTER_KEY_ID_RULE",
        "CKV_AWS_119",
        "DYNAMODB_TABLE_ENCRYPTED_KMS",
    ):
        assert marker not in raw, f"fixed encryption finding is still suppressed: {marker}"
