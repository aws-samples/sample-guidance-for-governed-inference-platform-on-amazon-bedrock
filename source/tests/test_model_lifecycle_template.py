# ABOUTME: Contract tests for deployment/infrastructure/model-lifecycle.yaml
# ABOUTME: Resources, scoped IAM, alert-topic reuse condition, Health rule, A5 alarm

"""Template contract tests for the model-lifecycle stack (R14 §2.3 / §5).

Validates the CloudFormation template's structure without deploying:
resource set, IAM scoping (actions match the Lambda's API calls), the
CreateTopic condition, the permissive aws.health passthrough rule, the
A5 fail-visible Lambda error alarm, and partition awareness.
"""

import json
import re
from pathlib import Path

import yaml

TEMPLATE_PATH = Path(__file__).parent.parent.parent / "deployment" / "infrastructure" / "model-lifecycle.yaml"
LAMBDA_PATH = (
    Path(__file__).parent.parent.parent
    / "deployment"
    / "infrastructure"
    / "lambda-functions"
    / "model_lifecycle"
    / "index.py"
)


class _CfnLoader(yaml.SafeLoader):
    pass


def _tag(name):
    def construct(loader, node):
        if isinstance(node, yaml.SequenceNode):
            return {name: loader.construct_sequence(node)}
        if isinstance(node, yaml.MappingNode):
            return {name: loader.construct_mapping(node)}
        value = loader.construct_scalar(node)
        if name == "GetAtt":
            return {name: value.split(".", 1)}
        return {name: value}

    return construct


for short in ("Ref", "GetAtt", "Sub", "If", "Join", "Equals", "Not", "And", "Or", "Select", "Split", "Condition"):
    _CfnLoader.add_constructor(f"!{short}", _tag(short))


def load_template() -> dict:
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        return yaml.load(f, Loader=_CfnLoader)  # nosec B506


class TestResources:
    def test_expected_resource_set(self):
        resources = load_template()["Resources"]
        assert set(resources) == {
            # SNS encryption (security review): stack-owned CMK + alias, and the
            # publisher grants for the check Lambda (created / external key).
            "LifecycleAlertTopicKey",
            "LifecycleAlertTopicKeyAlias",
            "LifecycleCheckKmsPolicy",
            "LifecycleCheckExternalKmsPolicy",
            "LifecycleAlertTopic",
            "LifecycleAlertTopicPolicy",
            "TrackedModelsParam",
            "AlertStateParam",
            "LifecycleCheckRole",
            "LifecycleCheckFunction",
            # ws4 scan hardening (LAMBDA_DLQ_CHECK): EventBridge invokes the
            # check async, so failed invocations land in an SSE-SQS DLQ.
            "LifecycleCheckDLQ",
            "DailyScheduleRule",
            "DailySchedulePermission",
            "HealthEventsRule",
            "LifecycleCheckErrorAlarm",
        }

    def test_topic_only_created_when_no_external_arn(self):
        template = load_template()
        assert template["Conditions"]["CreateTopic"] == {"Equals": [{"Ref": "AlertTopicArn"}, ""]}
        for logical_id in (
            "LifecycleAlertTopic",
            "LifecycleAlertTopicPolicy",
            "LifecycleAlertTopicKey",
            "LifecycleAlertTopicKeyAlias",
            "LifecycleCheckKmsPolicy",
        ):
            assert template["Resources"][logical_id]["Condition"] == "CreateTopic", logical_id

    def test_ssm_parameters_under_profile_path(self):
        resources = load_template()["Resources"]
        tracked = resources["TrackedModelsParam"]["Properties"]
        state = resources["AlertStateParam"]["Properties"]
        assert tracked["Name"] == {"Sub": "/gip/${ProfileName}/tracked-models"}
        assert state["Name"] == {"Sub": "/gip/${ProfileName}/lifecycle-alert-state"}
        # Empty seeds — deploy overwrites tracked-models out-of-band.
        assert json.loads(tracked["Value"]) == []
        assert json.loads(state["Value"]) == {}

    def test_lambda_env_wires_all_parameters(self):
        env = load_template()["Resources"]["LifecycleCheckFunction"]["Properties"]["Environment"]["Variables"]
        assert set(env) == {
            "TRACKED_MODELS_PARAM",
            "ALERT_STATE_PARAM",
            "SNS_TOPIC_ARN",
            "LEGACY_PREMIUM_WARNING_DAYS",
            "EOL_WARNING_DAYS",
            "CHECK_REGIONS",
        }
        assert env["SNS_TOPIC_ARN"] == {"If": ["CreateTopic", {"Ref": "LifecycleAlertTopic"}, {"Ref": "AlertTopicArn"}]}

    def test_daily_schedule_targets_lambda(self):
        resources = load_template()["Resources"]
        rule = resources["DailyScheduleRule"]["Properties"]
        assert rule["ScheduleExpression"] == {"Ref": "ScheduleExpression"}
        assert rule["Targets"][0]["Arn"] == {"GetAtt": ["LifecycleCheckFunction", "Arn"]}
        permission = resources["DailySchedulePermission"]["Properties"]
        assert permission["Principal"] == "events.amazonaws.com"
        assert permission["SourceArn"] == {"GetAtt": ["DailyScheduleRule", "Arn"]}


class TestIamScoping:
    def _raw_statements(self):
        role = load_template()["Resources"]["LifecycleCheckRole"]["Properties"]
        return role["Policies"][0]["PolicyDocument"]["Statement"]

    def _statements(self):
        """Statements with Fn::If wrappers expanded to both branches."""
        flat = []
        for stmt in self._raw_statements():
            if "If" in stmt:
                flat.extend(branch for branch in stmt["If"][1:] if isinstance(branch, dict))
            else:
                flat.append(stmt)
        return flat

    def test_actions_match_lambda_api_calls(self):
        """IAM actions correspond exactly to the boto3 calls in index.py (iam-actions rule).

        sqs:SendMessage is the one deliberate exception: the Lambda *service*
        (not index.py) uses the execution role to deliver failed async
        invocations to the DLQ. KMS topic grants live in conditional managed
        policies, not this inline policy.
        """
        actions = {a for stmt in self._statements() for a in stmt["Action"]}
        assert actions == {
            "bedrock:ListFoundationModels",
            "bedrock:GetFoundationModel",
            "ssm:GetParameter",
            "ssm:PutParameter",
            "sns:Publish",
            "sqs:SendMessage",
        }
        code = LAMBDA_PATH.read_text(encoding="utf-8")
        assert "list_foundation_models" in code
        assert "get_parameter" in code
        assert "put_parameter" in code
        assert "publish" in code

    def test_sqs_send_scoped_to_dlq(self):
        """The DLQ grant must cover exactly the DLQ, nothing else."""
        sqs_stmt = next(s for s in self._statements() if "sqs:SendMessage" in s["Action"])
        assert sqs_stmt["Action"] == ["sqs:SendMessage"]
        assert sqs_stmt["Resource"] == {"GetAtt": ["LifecycleCheckDLQ", "Arn"]}

    def test_function_dead_letter_config_targets_dlq(self):
        fn = load_template()["Resources"]["LifecycleCheckFunction"]["Properties"]
        assert fn["DeadLetterConfig"]["TargetArn"] == {"GetAtt": ["LifecycleCheckDLQ", "Arn"]}

    def test_ssm_scoped_to_the_two_parameters(self):
        get_stmt = next(s for s in self._statements() if "ssm:GetParameter" in s["Action"])
        resources = get_stmt["Resource"]
        assert len(resources) == 2
        for r in resources:
            assert r["Sub"].startswith("arn:${AWS::Partition}:ssm:${AWS::Region}:${AWS::AccountId}:parameter")

    def test_put_parameter_limited_to_alert_state(self):
        """The Lambda must not be able to rewrite the tracked-models input."""
        put_stmt = next(s for s in self._statements() if "ssm:PutParameter" in s["Action"])
        assert len(put_stmt["Resource"]) == 1
        assert "AlertStateParam" in str(put_stmt["Resource"][0])

    def test_sns_publish_scoped_to_topic(self):
        sns_stmt = next(s for s in self._statements() if "sns:Publish" in s["Action"])
        assert sns_stmt["Resource"] == {"If": ["CreateTopic", {"Ref": "LifecycleAlertTopic"}, {"Ref": "AlertTopicArn"}]}

    def test_no_wildcard_actions(self):
        for stmt in self._statements():
            for action in stmt["Action"]:
                assert action != "*" and not action.endswith(":*")

    def test_partition_aware_no_hardcoded_arns(self):
        raw = TEMPLATE_PATH.read_text(encoding="utf-8")
        assert not re.search(r"arn:aws:", raw), "hardcode 'arn:aws:' breaks GovCloud/China — use ${AWS::Partition}"


class TestHealthRuleAndAlarm:
    def test_health_rule_is_permissive_service_plus_category(self):
        """R14 §7: exact Bedrock eventTypeCodes are unverified — filter must stay permissive."""
        pattern = load_template()["Resources"]["HealthEventsRule"]["Properties"]["EventPattern"]
        assert pattern == {
            "source": ["aws.health"],
            "detail": {"service": ["BEDROCK"], "eventTypeCategory": ["scheduledChange"]},
        }
        raw = TEMPLATE_PATH.read_text(encoding="utf-8")
        assert "eventTypeCode" in raw  # the unverified-codes caveat is documented in comments

    def test_error_alarm_is_fail_visible(self):
        """A5: a broken poller must alarm to the same topic, never fail silently."""
        alarm = load_template()["Resources"]["LifecycleCheckErrorAlarm"]["Properties"]
        assert alarm["Namespace"] == "AWS/Lambda"
        assert alarm["MetricName"] == "Errors"
        assert alarm["Threshold"] == 0
        assert alarm["ComparisonOperator"] == "GreaterThanThreshold"
        assert alarm["TreatMissingData"] == "notBreaching"
        assert alarm["AlarmActions"] == [
            {"If": ["CreateTopic", {"Ref": "LifecycleAlertTopic"}, {"Ref": "AlertTopicArn"}]}
        ]

    def test_topic_policy_allows_eventbridge_and_cloudwatch_publish(self):
        """Both service publishers (Health rule, error alarm) need an explicit
        topic grant once the topic is CMK-encrypted (round-3 D6)."""
        resources = load_template()["Resources"]
        policy = resources["LifecycleAlertTopicPolicy"]["Properties"]["PolicyDocument"]
        by_principal = {stmt["Principal"]["Service"]: stmt for stmt in policy["Statement"]}
        expected = _service_publishers(resources)
        assert expected == {"events.amazonaws.com", "cloudwatch.amazonaws.com"}
        assert set(by_principal) == expected
        for stmt in by_principal.values():
            assert stmt["Effect"] == "Allow"
            assert stmt["Action"] == "sns:Publish"
            assert stmt["Resource"] == {"Ref": "LifecycleAlertTopic"}
            assert stmt["Condition"]["StringEquals"]["aws:SourceAccount"] == {"Ref": "AWS::AccountId"}

    def test_dlq_uses_the_aws_managed_sqs_key(self):
        """Round-3 D9: KmsMasterKeyId (what CKV_AWS_27 / the guard rule read)
        with the AWS-managed key - the only writer is the Lambda service using
        the execution role, which aws/sqs already covers; no IAM change."""
        queue = load_template()["Resources"]["LifecycleCheckDLQ"]
        assert queue["Properties"]["KmsMasterKeyId"] == "alias/aws/sqs"
        assert "SqsManagedSseEnabled" not in queue["Properties"]
        assert "Metadata" not in queue


TOPIC_REF = {"If": ["CreateTopic", {"Ref": "LifecycleAlertTopic"}, {"Ref": "AlertTopicArn"}]}
KMS_PUBLISHER_ACTIONS = {"kms:GenerateDataKey", "kms:Decrypt"}


def _service_publishers(resources: dict) -> set[str]:
    """Service principals that publish to the alert topic, derived from the template.

    EventBridge rules whose target is the topic -> events.amazonaws.com;
    CloudWatch alarms whose AlarmActions include the topic -> cloudwatch.amazonaws.com.
    The key policy must grant exactly these (SNS Developer Guide, "Enable
    compatibility between event sources from AWS services and encrypted topics").
    """
    publishers = set()
    for resource in resources.values():
        props = resource.get("Properties", {})
        if resource["Type"] == "AWS::Events::Rule" and any(t["Arn"] == TOPIC_REF for t in props.get("Targets", [])):
            publishers.add("events.amazonaws.com")
        if resource["Type"] == "AWS::CloudWatch::Alarm" and TOPIC_REF in props.get("AlarmActions", []):
            publishers.add("cloudwatch.amazonaws.com")
    return publishers


def _key_statements() -> dict[str, dict]:
    statements = load_template()["Resources"]["LifecycleAlertTopicKey"]["Properties"]["KeyPolicy"]["Statement"]
    return {s["Sid"]: s for s in statements}


class TestTopicEncryption:
    """security review: the created alert topic is encrypted with a stack-owned CMK.

    The AWS-managed aws/sns key cannot be used because EventBridge and
    CloudWatch alarms need key-policy grants (fixed policy on aws/sns), and a
    missing grant makes those publishers fail silently - hence the publisher
    set is derived from the template and compared with the key policy.
    """

    def test_topic_encrypted_with_stack_key_and_name_unchanged(self):
        topic = load_template()["Resources"]["LifecycleAlertTopic"]["Properties"]
        assert topic["KmsMasterKeyId"] == {"Ref": "LifecycleAlertTopicKey"}
        # Same TopicName as before encryption: KmsMasterKeyId updates in place,
        # so existing subscriptions survive the stack update.
        assert topic["TopicName"] == "gip-model-lifecycle-alerts"

    def test_key_hygiene(self):
        resources = load_template()["Resources"]
        key = resources["LifecycleAlertTopicKey"]
        assert key["Type"] == "AWS::KMS::Key"
        assert key["Properties"]["EnableKeyRotation"] is True
        assert key["Properties"]["PendingWindowInDays"] == 7
        assert "gip-model-lifecycle-alerts" in key["Properties"]["Description"]
        alias = resources["LifecycleAlertTopicKeyAlias"]
        assert alias["Type"] == "AWS::KMS::Alias"
        assert alias["Properties"]["AliasName"] == {"Sub": "alias/${AWS::StackName}-lifecycle-alerts"}
        assert alias["Properties"]["TargetKeyId"] == {"Ref": "LifecycleAlertTopicKey"}

    def test_key_policy_grants_exactly_the_service_publishers(self):
        statements = _key_statements()
        admin = statements["AccountAdmin"]
        assert admin["Principal"] == {"AWS": {"Sub": "arn:${AWS::Partition}:iam::${AWS::AccountId}:root"}}
        assert admin["Action"] == "kms:*"

        service_statements = [s for s in statements.values() if "Service" in s.get("Principal", {})]
        granted = {s["Principal"]["Service"] for s in service_statements}
        expected = _service_publishers(load_template()["Resources"])
        assert expected == {"events.amazonaws.com", "cloudwatch.amazonaws.com"}  # Health rule + error alarm
        # SNS itself is granted (encryption-context bound); every actual publisher is granted.
        assert granted == expected | {"sns.amazonaws.com"}
        for stmt in service_statements:
            assert stmt["Effect"] == "Allow"
            assert stmt["Resource"] == "*"
            assert "kms:Decrypt" in stmt["Action"]
            assert any(a.startswith("kms:GenerateDataKey") for a in stmt["Action"])
            assert not any(a == "kms:*" or a.endswith(":*") for a in stmt["Action"])

    def test_eventbridge_statement_has_no_source_condition(self):
        """SNS Developer Guide: aws:SourceAccount/SourceArn/SourceOrgID are not
        supported for EventBridge-to-encrypted-topic delivery - a condition here
        would make the Health rule fail silently."""
        events = next(
            s for s in _key_statements().values() if s.get("Principal") == {"Service": "events.amazonaws.com"}
        )
        assert "Condition" not in events

    def test_cloudwatch_statement_is_account_scoped(self):
        cloudwatch = next(
            s for s in _key_statements().values() if s.get("Principal") == {"Service": "cloudwatch.amazonaws.com"}
        )
        assert cloudwatch["Condition"] == {"StringEquals": {"aws:SourceAccount": {"Ref": "AWS::AccountId"}}}

    def test_sns_statement_bound_to_this_topic(self):
        sns = next(s for s in _key_statements().values() if s.get("Principal") == {"Service": "sns.amazonaws.com"})
        context = sns["Condition"]["StringEquals"]["kms:EncryptionContext:aws:sns:topicArn"]
        assert context == {
            "Sub": "arn:${AWS::Partition}:sns:${AWS::Region}:${AWS::AccountId}:gip-model-lifecycle-alerts"
        }

    def test_lambda_publisher_granted_on_created_key(self):
        """Every role that publishes to the created topic gets the publisher KMS grant."""
        resources = load_template()["Resources"]
        publishing_roles = {
            name
            for name, res in resources.items()
            if res["Type"] == "AWS::IAM::Role"
            and any(
                "sns:Publish" in s["Action"]
                for p in res["Properties"]["Policies"]
                for s in p["PolicyDocument"]["Statement"]
            )
        }
        assert publishing_roles == {"LifecycleCheckRole"}

        policy = resources["LifecycleCheckKmsPolicy"]
        assert policy["Type"] == "AWS::IAM::ManagedPolicy"
        assert policy["Condition"] == "CreateTopic"
        assert policy["Properties"]["Roles"] == [{"Ref": "LifecycleCheckRole"}]
        (stmt,) = policy["Properties"]["PolicyDocument"]["Statement"]
        assert set(stmt["Action"]) == KMS_PUBLISHER_ACTIONS
        assert stmt["Resource"] == {"GetAtt": ["LifecycleAlertTopicKey", "Arn"]}

        # The inline role policy carries no KMS grant of its own (no !If
        # statements inside the role - keeps the statement list scanner-plain).
        inline_actions = {
            a
            for p in resources["LifecycleCheckRole"]["Properties"]["Policies"]
            for s in p["PolicyDocument"]["Statement"]
            for a in s["Action"]
        }
        assert not any(a.startswith("kms:") for a in inline_actions)

    def test_external_encrypted_topic_grant_is_opt_in(self):
        """BYO encrypted topic (e.g. the quota stack's topic, wired by gip deploy):
        the key ARN parameter grants the same publisher actions; nothing is
        granted for an unencrypted external topic."""
        template = load_template()
        param = template["Parameters"]["AlertTopicKmsKeyArn"]
        assert param["Default"] == ""
        assert param["AllowedPattern"].startswith("^$|^arn:")
        assert template["Conditions"]["HasAlertTopicKmsKey"] == {
            "Not": [{"Equals": [{"Ref": "AlertTopicKmsKeyArn"}, ""]}]
        }
        assert template["Conditions"]["UsesExternalTopicKey"] == {
            "And": [{"Not": [{"Condition": "CreateTopic"}]}, {"Condition": "HasAlertTopicKmsKey"}]
        }
        policy = template["Resources"]["LifecycleCheckExternalKmsPolicy"]
        assert policy["Condition"] == "UsesExternalTopicKey"
        assert policy["Properties"]["Roles"] == [{"Ref": "LifecycleCheckRole"}]
        (stmt,) = policy["Properties"]["PolicyDocument"]["Statement"]
        assert set(stmt["Action"]) == KMS_PUBLISHER_ACTIONS
        assert stmt["Resource"] == {"Ref": "AlertTopicKmsKeyArn"}

    def test_encryption_suppressions_removed(self):
        raw = TEMPLATE_PATH.read_text(encoding="utf-8")
        for marker in ("CKV_AWS_26", "SNS_ENCRYPTED_KMS", "D1 decision memo"):
            assert marker not in raw, f"SNS encryption suppression still present: {marker}"
        for marker in ("CKV_AWS_27", "SQS_QUEUE_KMS_MASTER_KEY_ID_RULE"):
            assert marker not in raw, f"DLQ encryption suppression still present: {marker}"


class TestParameters:
    def test_optional_parameters_have_defaults(self):
        """Backwards compat: only ProfileName may be mandatory."""
        params = load_template()["Parameters"]
        for name, spec in params.items():
            if name == "ProfileName":
                continue
            assert "Default" in spec, f"parameter {name} needs a Default"

    def test_threshold_defaults_match_lambda_and_cli(self):
        """Template defaults, Lambda env fallbacks, and catalog_check constants must agree."""
        from governed_inference_platform.catalog_check import EOL_CRITICAL_DAYS, PREMIUM_WARNING_DAYS

        params = load_template()["Parameters"]
        assert params["LegacyPremiumWarningDays"]["Default"] == PREMIUM_WARNING_DAYS == 30
        eol_days = [int(d) for d in str(params["EolWarningDays"]["Default"]).split(",")]
        assert eol_days == [60, 30, 7]
        assert max(eol_days) == EOL_CRITICAL_DAYS
        code = LAMBDA_PATH.read_text(encoding="utf-8")
        assert '"60,30,7"' in code
        assert '"30"' in code
