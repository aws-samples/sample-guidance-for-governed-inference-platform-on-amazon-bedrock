# ABOUTME: Contract tests for deployment/infrastructure/skills-registry.yaml
# ABOUTME: Pins bucket hardening, custom-resource wiring, persona IAM, EventBridge, and distributor env

"""Template contract tests for the skills registry stack (lane E-S1).

These pin the security posture and the integration contracts other components
rely on: the artifact bucket layout/hardening, the Custom::AgentRegistry
custom resource (no CFN type exists — verified 2026-07-08), the pending-
approval EventBridge wiring, and the distributor's PLUGINS_S3_KEY (the exact
key the retired bootstrap-device-code Lambda read — template removed from the
repo, not included in this sample; kept stable for deployed legacy stacks).
"""

from pathlib import Path

import yaml

TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "deployment" / "infrastructure" / "skills-registry.yaml"
LAMBDA_REQUIREMENTS = TEMPLATE_PATH.parent / "lambda-functions" / "skills_registry" / "requirements.txt"


class CFLoader(yaml.SafeLoader):
    """YAML loader that tolerates CloudFormation intrinsic function tags."""


for tag in (
    "!Ref",
    "!Sub",
    "!GetAtt",
    "!Join",
    "!Select",
    "!Split",
    "!If",
    "!Not",
    "!Equals",
    "!And",
    "!Or",
    "!FindInMap",
    "!Base64",
    "!Cidr",
    "!ImportValue",
    "!GetAZs",
    "!Condition",
):
    CFLoader.add_constructor(
        tag,
        lambda loader, node: (
            loader.construct_scalar(node)
            if isinstance(node, yaml.ScalarNode)
            else loader.construct_sequence(node, deep=True)
            if isinstance(node, yaml.SequenceNode)
            else loader.construct_mapping(node, deep=True)
        ),
    )


def _template() -> dict:
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        return yaml.load(f, Loader=CFLoader)  # nosec B506


def _resources() -> dict:
    return _template()["Resources"]


def _flatten_statements(statements: list) -> list[dict]:
    flattened = []
    for statement in statements:
        if isinstance(statement, dict):
            flattened.append(statement)
        elif isinstance(statement, list):
            flattened.extend(branch for branch in statement[1:] if isinstance(branch, dict))
    return flattened


def _role_statements(role: dict) -> list[dict]:
    return [
        statement
        for policy in role["Properties"]["Policies"]
        for statement in _flatten_statements(policy["PolicyDocument"]["Statement"])
    ]


# ---------------------------------------------------------------------------
# Parameters and conditions
# ---------------------------------------------------------------------------


def test_parameters_have_safe_defaults():
    """Every parameter defaults to the off/single-account posture."""
    params = _template()["Parameters"]
    assert params["OrganizationId"]["Default"] == ""  # org read is opt-in
    assert params["PluginsS3Bucket"]["Default"] == ""
    assert params["PublisherGroups"]["Default"] == ""
    assert params["CuratorGroups"]["Default"] == ""
    assert params["RetainRegistryOnDelete"]["Default"] == "true"
    assert params["RegistryName"]["Default"] == "gip-skills"


def test_organization_id_pattern_guards_input():
    params = _template()["Parameters"]
    assert params["OrganizationId"]["AllowedPattern"] == "^$|^o-[a-z0-9]{10,32}$"


def test_group_params_are_comma_delimited_lists():
    """Mirrors the gateway lane's EntitledGroups parameter pattern."""
    params = _template()["Parameters"]
    assert params["PublisherGroups"]["Type"] == "CommaDelimitedList"
    assert params["CuratorGroups"]["Type"] == "CommaDelimitedList"


# ---------------------------------------------------------------------------
# Artifact bucket hardening
# ---------------------------------------------------------------------------


def test_artifact_bucket_hardening():
    bucket = _resources()["SkillsArtifactBucket"]["Properties"]
    assert bucket["VersioningConfiguration"]["Status"] == "Enabled"
    sse = bucket["BucketEncryption"]["ServerSideEncryptionConfiguration"][0]
    assert sse["ServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"
    pab = bucket["PublicAccessBlockConfiguration"]
    assert all(pab[k] for k in ("BlockPublicAcls", "BlockPublicPolicy", "IgnorePublicAcls", "RestrictPublicBuckets"))
    # Access logging to the dedicated logging bucket (repo bucket pattern)
    assert bucket["LoggingConfiguration"]["DestinationBucketName"] == "SkillsLoggingBucket"


def test_bucket_policy_denies_insecure_transport_unconditionally():
    statements = _resources()["SkillsArtifactBucketPolicy"]["Properties"]["PolicyDocument"]["Statement"]
    deny = statements[0]
    assert deny["Sid"] == "DenyInsecureTransport"
    assert deny["Effect"] == "Deny"
    # Boolean false (not string 'false'): the S3_BUCKET_SSL_REQUESTS_ONLY
    # guard rule compares against a YAML boolean; IAM accepts either form.
    assert deny["Condition"] == {"Bool": {"aws:SecureTransport": False}}
    # The TLS-only deny must NOT be behind the org condition
    assert not isinstance(deny.get("Principal"), list)


def test_approved_artifact_writes_require_create_only_precondition():
    statements = _flatten_statements(
        _resources()["SkillsArtifactBucketPolicy"]["Properties"]["PolicyDocument"]["Statement"]
    )
    deny = next(statement for statement in statements if statement.get("Sid") == "DenyUnconditionalApprovedWrites")
    assert deny["Effect"] == "Deny"
    assert deny["Action"] == "s3:PutObject"
    assert "approved/*" in str(deny["Resource"])
    assert deny["Condition"] == {"Null": {"s3:if-none-match": "true"}}


def test_approved_artifacts_cannot_be_deleted_or_recreated():
    statements = _flatten_statements(
        _resources()["SkillsArtifactBucketPolicy"]["Properties"]["PolicyDocument"]["Statement"]
    )
    deny = next(statement for statement in statements if statement.get("Sid") == "DenyApprovedArtifactDeletion")
    assert deny["Effect"] == "Deny"
    assert sorted(deny["Action"]) == ["s3:DeleteObject", "s3:DeleteObjectVersion"]
    assert "approved/*" in str(deny["Resource"])


def test_review_source_writes_require_create_only_precondition():
    statements = _flatten_statements(
        _resources()["SkillsArtifactBucketPolicy"]["Properties"]["PolicyDocument"]["Statement"]
    )
    deny = next(statement for statement in statements if statement.get("Sid") == "DenyUnconditionalReviewSourceWrites")
    assert deny["Effect"] == "Deny"
    assert deny["Action"] == "s3:PutObject"
    assert "/skills/*" in str(deny["Resource"])
    assert deny["Condition"] == {"Null": {"s3:if-none-match": "true"}}


def test_org_read_statement_is_conditional_and_read_only():
    """Organization consumers see approved bytes, never mutable review sources."""
    statements = _resources()["SkillsArtifactBucketPolicy"]["Properties"]["PolicyDocument"]["Statement"]
    org_conditions = [statement for statement in statements if isinstance(statement, list) and statement[0] == "IsOrg"]
    assert len(org_conditions) == 2
    org = {statement[1]["Sid"]: statement[1] for statement in org_conditions}
    read = org["OrgWideApprovedRead"]
    assert sorted(read["Action"]) == ["s3:GetObject", "s3:GetObjectVersion"]
    assert all("approved/*" in str(resource) or "distribution/*" in str(resource) for resource in read["Resource"])
    assert not any("/skills/*" in str(resource) for resource in read["Resource"])
    listing = org["OrgWideApprovedList"]
    assert listing["Action"] == "s3:ListBucket"
    assert listing["Condition"]["StringLike"]["s3:prefix"] == ["approved/*", "distribution/*"]


# ---------------------------------------------------------------------------
# Custom resource (no AWS::BedrockAgentCore::Registry type exists)
# ---------------------------------------------------------------------------


def test_registry_is_a_lambda_backed_custom_resource():
    resources = _resources()
    registry = resources["SkillsRegistry"]
    assert registry["Type"] == "Custom::AgentRegistry"
    props = registry["Properties"]
    assert props["AuthorizerType"] == "AWS_IAM"  # governance registry is IAM-mode
    assert props["ServiceToken"] == "RegistryProvisionerFunction.Arn"
    # Registry survives stack deletion by default (approval state lives there)
    assert props["RetainOnDelete"] == "RetainRegistryOnDelete"


def test_provisioner_and_distributor_share_the_registry_client_module():
    """Both functions package the same Code dir so registry_client.py stays
    the single namespace-migration change point."""
    resources = _resources()
    provisioner = resources["RegistryProvisionerFunction"]["Properties"]
    distributor = resources["DistributorFunction"]["Properties"]
    assert provisioner["Code"] == "./lambda-functions/skills_registry/"
    assert distributor["Code"] == "./lambda-functions/skills_registry/"
    assert provisioner["Handler"] == "index.handler"
    assert distributor["Handler"] == "distributor.handler"
    assert provisioner["Runtime"] == distributor["Runtime"] == "python3.12"


def test_lambda_package_pins_ga_capable_boto3():
    assert LAMBDA_REQUIREMENTS.read_text(encoding="utf-8").splitlines() == [
        "boto3==1.43.70",
        "botocore==1.43.70",
    ]


# ---------------------------------------------------------------------------
# IAM personas
# ---------------------------------------------------------------------------


def _policy_actions(role: dict) -> list[str]:
    actions = []
    for stmt in _role_statements(role):
        act = stmt.get("Action", [])
        actions.extend([act] if isinstance(act, str) else act)
    return actions


def test_publisher_can_submit_but_never_approve():
    actions = _policy_actions(_resources()["PublisherRole"])
    for prefix in ("bedrock-agentcore", "agent-registry"):
        assert f"{prefix}:SubmitRegistryRecordForApproval" in actions
        assert f"{prefix}:CreateRegistryRecord" in actions
        assert f"{prefix}:UpdateRegistryRecord" not in actions
        assert f"{prefix}:UpdateRegistryRecordStatus" not in actions


def test_publisher_cannot_write_or_delete_approved_artifacts():
    statements = _role_statements(_resources()["PublisherRole"])
    upload = next(statement for statement in statements if statement.get("Sid") == "UploadArtifacts")
    actions = upload["Action"] if isinstance(upload["Action"], list) else [upload["Action"]]
    assert set(actions) == {"s3:PutObject", "s3:GetObject"}
    assert "/skills/*" in str(upload["Resource"])
    assert "/approved/" not in str(upload["Resource"])
    assert not any(
        action in _policy_actions(_resources()["PublisherRole"])
        for action in ("s3:DeleteObject", "s3:DeleteObjectVersion")
    )
    listing = next(statement for statement in statements if statement.get("Sid") == "ListArtifacts")
    assert listing["Condition"] == {"StringLike": {"s3:prefix": "skills/*"}}


def test_distributor_has_only_required_promotion_permissions():
    statements = _role_statements(_resources()["DistributorRole"])
    source = next(statement for statement in statements if statement.get("Sid") == "ReadReviewSourceVersions")
    source_listing = next(statement for statement in statements if statement.get("Sid") == "ListReviewSourceDirectory")
    versions = next(statement for statement in statements if statement.get("Sid") == "ListReviewSourceVersions")
    approved_listing = next(statement for statement in statements if statement.get("Sid") == "ListApprovedDirectory")
    promote = next(statement for statement in statements if statement.get("Sid") == "PromoteApprovedArtifacts")
    assert set(source["Action"]) == {"s3:GetObject", "s3:GetObjectVersion"}
    assert "/skills/*" in str(source["Resource"])
    assert source_listing["Action"] == "s3:ListBucket"
    assert source_listing["Condition"] == {"StringLike": {"s3:prefix": "skills/*"}}
    assert versions["Action"] == "s3:ListBucketVersions"
    assert versions["Condition"] == {"StringLike": {"s3:prefix": "skills/*"}}
    assert approved_listing["Action"] == "s3:ListBucket"
    assert approved_listing["Condition"] == {"StringLike": {"s3:prefix": "approved/sha256/*"}}
    assert set(promote["Action"]) == {"s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"}
    assert "/approved/sha256/*" in str(promote["Resource"])
    assert "s3:DeleteObject" not in _policy_actions(_resources()["DistributorRole"])


def test_distributor_mediates_registry_status_after_promotion():
    actions = _policy_actions(_resources()["DistributorRole"])
    for prefix in ("bedrock-agentcore", "agent-registry"):
        assert f"{prefix}:UpdateRegistryRecord" in actions
        assert f"{prefix}:SubmitRegistryRecordForApproval" in actions
        assert f"{prefix}:UpdateRegistryRecordStatus" in actions


def test_curator_must_invoke_workflow_and_cannot_transition_status_directly():
    actions = _policy_actions(_resources()["CuratorRole"])
    assert "lambda:InvokeFunction" in actions
    for prefix in ("bedrock-agentcore", "agent-registry"):
        assert f"{prefix}:UpdateRegistryRecordStatus" not in actions
        assert f"{prefix}:UpdateRegistryRecord" not in actions
        assert f"{prefix}:CreateRegistryRecord" not in actions
    assert "s3:PutObject" not in actions


def test_persona_roles_gate_on_principal_tag_groups_when_groups_set():
    for role_name, condition in (("PublisherRole", "HasPublisherGroups"), ("CuratorRole", "HasCuratorGroups")):
        trust = _resources()[role_name]["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]
        gate = trust["Condition"]
        assert gate[0] == condition
        assert gate[1] == {
            "StringEquals": {
                "aws:PrincipalTag/groups": "PublisherGroups" if "Publisher" in role_name else "CuratorGroups"
            }
        }


def test_no_wildcard_iam_actions():
    """No role in the template uses Action:'*' or service:* wildcards."""
    for name, resource in _resources().items():
        if resource.get("Type") != "AWS::IAM::Role":
            continue
        for action in _policy_actions(resource):
            assert action != "*", f"{name} grants Action:'*'"
            # s3:* etc. — DenyInsecureTransport lives in the bucket policy, not roles
            assert not action.endswith(":*"), f"{name} grants {action}"


# ---------------------------------------------------------------------------
# Approval workflow + distributor contracts
# ---------------------------------------------------------------------------


def test_pending_approval_event_rule_matches_documented_event():
    """The only documented registry event (registry-key-capabilities,
    retrieved 2026-07-08). Both namespaces are matched until the preview
    endpoints shut down 2026-09-17 (ADR-0029 day-one execution)."""
    rule = _resources()["PendingApprovalRule"]["Properties"]
    assert rule["EventPattern"]["source"] == ["aws.bedrock-agentcore", "aws.agent-registry"]
    assert rule["EventPattern"]["detail-type"] == ["Registry Record State changed to Pending Approval"]
    # BYO-topic support: the rule targets the created topic or the
    # customer-supplied (e.g. pre-encrypted) AlertTopicArn.
    assert rule["Targets"][0]["Arn"] == ["CreateTopic", "CuratorTopic", "AlertTopicArn"]


# ---------------------------------------------------------------------------
# security review: one stack CMK encrypts the curator topic and distributor DLQ
# ---------------------------------------------------------------------------

CURATOR_TOPIC_REF = ["CreateTopic", "CuratorTopic", "AlertTopicArn"]
DISTRIBUTOR_DLQ_REF = "DistributorDeadLetterQueue.Arn"


def _curator_topic_service_publishers(resources: dict) -> set[str]:
    """Service principals that publish to the curator topic, derived from the template."""
    publishers = set()
    for resource in resources.values():
        props = resource.get("Properties", {})
        if resource["Type"] == "AWS::Events::Rule" and any(
            t["Arn"] == CURATOR_TOPIC_REF for t in props.get("Targets", [])
        ):
            publishers.add("events.amazonaws.com")
        if resource["Type"] == "AWS::CloudWatch::Alarm" and CURATOR_TOPIC_REF in props.get("AlarmActions", []):
            publishers.add("cloudwatch.amazonaws.com")
    return publishers


def _distributor_dlq_service_publishers(resources: dict) -> set[str]:
    """Service principals that write to the distributor DLQ, derived from rule targets."""
    publishers = set()
    for resource in resources.values():
        if resource["Type"] != "AWS::Events::Rule":
            continue
        for target in resource.get("Properties", {}).get("Targets", []):
            if target.get("DeadLetterConfig", {}).get("Arn") == DISTRIBUTOR_DLQ_REF:
                publishers.add("events.amazonaws.com")
    return publishers


def test_curator_topic_and_distributor_dlq_share_the_only_stack_key():
    resources = _resources()
    keys = {name: resource for name, resource in resources.items() if resource["Type"] == "AWS::KMS::Key"}
    assert set(keys) == {"CuratorTopicKey"}

    key = keys["CuratorTopicKey"]
    assert "Condition" not in key  # the DLQ exists even when an external topic is supplied
    assert key["Properties"]["EnableKeyRotation"] is True
    assert key["Properties"]["PendingWindowInDays"] == 7
    assert "pending-approval" in key["Properties"]["Description"]

    alias = resources["CuratorTopicKeyAlias"]
    assert alias["Type"] == "AWS::KMS::Alias"
    assert "Condition" not in alias
    assert alias["Properties"]["AliasName"] == "alias/${AWS::StackName}-pending-approval"
    assert alias["Properties"]["TargetKeyId"] == "CuratorTopicKey"

    topic = resources["CuratorTopic"]
    assert topic["Condition"] == "CreateTopic"
    assert topic["Properties"]["KmsMasterKeyId"] == "CuratorTopicKey"
    assert topic["Properties"]["TopicName"] == "${AWS::StackName}-pending-approval"

    queue = resources["DistributorDeadLetterQueue"]
    assert "Condition" not in queue
    assert queue["Properties"]["KmsMasterKeyId"] == "CuratorTopicKey"
    assert "SqsManagedSseEnabled" not in queue["Properties"]
    assert "Metadata" not in queue


def test_curator_topic_key_policy_grants_exactly_the_service_publishers():
    """Derive service writers from the topic and DLQ resources, then pin the key policy."""
    resources = _resources()
    statements = {s["Sid"]: s for s in resources["CuratorTopicKey"]["Properties"]["KeyPolicy"]["Statement"]}
    assert set(statements) == {"AccountAdmin", "SnsServiceUse", "EventBridgePublish"}

    admin = statements["AccountAdmin"]
    assert admin["Principal"] == {"AWS": "arn:${AWS::Partition}:iam::${AWS::AccountId}:root"}
    assert admin["Action"] == "kms:*"

    service_statements = [s for s in statements.values() if "Service" in s.get("Principal", {})]
    granted = {s["Principal"]["Service"] for s in service_statements}
    expected = _curator_topic_service_publishers(resources) | _distributor_dlq_service_publishers(resources)
    assert expected == {"events.amazonaws.com"}
    assert granted == expected | {"sns.amazonaws.com"}
    for stmt in service_statements:
        assert stmt["Effect"] == "Allow"
        assert stmt["Resource"] == "*"
        assert "kms:Decrypt" in stmt["Action"]
        assert any(a.startswith("kms:GenerateDataKey") for a in stmt["Action"])
        assert not any(a == "kms:*" or a.endswith(":*") for a in stmt["Action"])

    # SNS Developer Guide: aws:Source* conditions are not supported for
    # EventBridge-to-encrypted-topic delivery; CuratorTopicPolicy (SourceArn =
    # the rule) is the confused-deputy control instead.
    events = statements["EventBridgePublish"]
    assert events["Principal"] == {"Service": "events.amazonaws.com"}
    assert "Condition" not in events
    topic_policy = resources["CuratorTopicPolicy"]["Properties"]["PolicyDocument"]["Statement"][0]
    assert topic_policy["Condition"] == {"ArnEquals": {"aws:SourceArn": "PendingApprovalRule.Arn"}}

    sns = statements["SnsServiceUse"]
    assert sns["Principal"] == {"Service": "sns.amazonaws.com"}
    assert sns["Condition"]["StringEquals"]["kms:EncryptionContext:aws:sns:topicArn"] == (
        "arn:${AWS::Partition}:sns:${AWS::Region}:${AWS::AccountId}:${AWS::StackName}-pending-approval"
    )


def test_only_distributor_role_gets_the_scoped_dlq_key_grant_and_no_role_publishes_to_sns():
    resources = _resources()
    kms_statements_by_role = {}
    for name, resource in resources.items():
        if resource.get("Type") != "AWS::IAM::Role":
            continue
        statements = _role_statements(resource)
        actions = _policy_actions(resource)
        assert "sns:Publish" not in actions, f"{name} must not publish to the curator topic"
        kms_statements = []
        for statement in statements:
            statement_actions = statement.get("Action", [])
            if isinstance(statement_actions, str):
                statement_actions = [statement_actions]
            if any(action.startswith("kms:") for action in statement_actions):
                kms_statements.append(statement)
        if kms_statements:
            kms_statements_by_role[name] = kms_statements

    assert set(kms_statements_by_role) == {"DistributorRole"}
    (key_use,) = kms_statements_by_role["DistributorRole"]
    assert key_use["Sid"] == "UseDeadLetterQueueKey"
    assert set(key_use["Action"]) == {"kms:Decrypt", "kms:GenerateDataKey"}
    assert key_use["Resource"] == "CuratorTopicKey.Arn"


def test_curator_topic_encryption_suppressions_removed():
    raw = TEMPLATE_PATH.read_text(encoding="utf-8")
    for marker in (
        "CKV_AWS_26",
        "SNS_ENCRYPTED_KMS",
        "D1 decision memo",
        "CKV_AWS_27",
        "SQS_QUEUE_KMS_MASTER_KEY_ID_RULE",
    ):
        assert marker not in raw, f"fixed encryption finding is still suppressed: {marker}"


def test_distributor_env_preserves_legacy_plugin_key_without_cli_wiring():
    """Keep the dormant key stable for old raw-CloudFormation consumers."""
    env = _resources()["DistributorFunction"]["Properties"]["Environment"]["Variables"]
    assert env["PLUGINS_S3_KEY"] == "plugins-registry.json"
    assert env["MARKETPLACE_S3_KEY"] == "distribution/marketplace.json"
    assert env["SKILLS_LOCK_S3_KEY"] == "distribution/skills-lock.json"
    assert env["REGISTRY_ID"] == "SkillsRegistry.RegistryId"


def test_distributor_scheduled_every_15_minutes():
    rule = _resources()["DistributorSchedule"]["Properties"]
    assert rule["ScheduleExpression"] == "rate(15 minutes)"


# ---------------------------------------------------------------------------
# ADR-0029 day-one execution (2026-08-12): GA API surface wiring
# ---------------------------------------------------------------------------


def test_api_surface_parameter_defaults_to_ga_with_preview_fallback():
    """The stack surface knob mirrors the code default (GA) and only allows
    the two pinned surfaces from ADR-0029."""
    param = _template()["Parameters"]["AgentRegistryApiSurface"]
    assert param["Default"] == "ga-2026-08-06"
    assert param["AllowedValues"] == ["preview-2026-07-08", "ga-2026-08-06"]


def test_both_lambdas_receive_the_api_surface_env_var():
    """GIP_AGENT_REGISTRY_API_SURFACE is the single cutover knob; both
    functions must receive it from the template parameter (ADR-0029 step 2)."""
    resources = _resources()
    for name in ("RegistryProvisionerFunction", "DistributorFunction"):
        env = resources[name]["Properties"]["Environment"]["Variables"]
        assert env["GIP_AGENT_REGISTRY_API_SURFACE"] == "AgentRegistryApiSurface", name


def test_registry_roles_grant_both_namespaces_until_preview_shutdown():
    """Dual grants (bedrock-agentcore: AND agent-registry:) so the surface
    parameter can select either namespace until the preview endpoints shut
    down on 2026-09-17; drop the bedrock-agentcore grants after that."""
    resources = _resources()
    for role_name, sid in (
        ("RegistryProvisionerRole", "RegistryLifecycle"),
        ("PublisherRole", "PublishRecords"),
        ("CuratorRole", "ReviewRecords"),
        ("DistributorRole", "ReadAndDecideRecords"),
    ):
        statement = next(s for s in _role_statements(resources[role_name]) if s.get("Sid") == sid)
        actions = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
        preview_actions = {a.split(":", 1)[1] for a in actions if a.startswith("bedrock-agentcore:")}
        ga_actions = {a.split(":", 1)[1] for a in actions if a.startswith("agent-registry:")}
        assert preview_actions == ga_actions, f"{role_name}/{sid} namespaces out of sync"
        resource_str = str(statement["Resource"])
        assert ":bedrock-agentcore:" in resource_str, f"{role_name}/{sid} missing preview ARNs"
        assert ":agent-registry:" in resource_str, f"{role_name}/{sid} missing GA ARNs"


def test_registry_first_use_permissions_match_service_authorization_contract():
    statements = {
        statement["Sid"]: statement
        for statement in _role_statements(_resources()["RegistryProvisionerRole"])
        if "Sid" in statement
    }

    collection = statements["RegistryCollectionLifecycle"]
    assert set(collection["Action"]) == {
        "bedrock-agentcore:CreateRegistry",
        "bedrock-agentcore:ListRegistries",
        "agent-registry:CreateRegistry",
        "agent-registry:ListRegistries",
    }
    assert collection["Resource"] == "*"
    assert collection["Condition"] == {"StringEquals": {"aws:RequestedRegion": "AWS::Region"}}

    lifecycle = statements["RegistryLifecycle"]
    assert set(lifecycle["Action"]) == {
        "bedrock-agentcore:GetRegistry",
        "bedrock-agentcore:UpdateRegistry",
        "bedrock-agentcore:DeleteRegistry",
        "agent-registry:GetRegistry",
        "agent-registry:UpdateRegistry",
        "agent-registry:DeleteRegistry",
    }
    assert all("registry/*" in resource for resource in lifecycle["Resource"])

    workload = statements["RegistryWorkloadIdentityLifecycle"]
    assert set(workload["Action"]) == {
        "bedrock-agentcore:CreateWorkloadIdentity",
        "bedrock-agentcore:GetWorkloadIdentity",
        "bedrock-agentcore:DeleteWorkloadIdentity",
    }
    assert workload["Resource"] == "*"
    assert workload["Condition"] == {"StringEquals": {"aws:RequestedRegion": "AWS::Region"}}

    service_role = statements["CreateAgentRegistryServiceLinkedRole"]
    assert service_role["Action"] == "iam:CreateServiceLinkedRole"
    assert "agent-registry.amazonaws.com/AWSServiceRoleForAgentRegistry" in service_role["Resource"]
    assert service_role["Condition"] == {"StringEquals": {"iam:AWSServiceName": "agent-registry.amazonaws.com"}}


def test_registry_arn_output_follows_the_selected_surface():
    output = _template()["Outputs"]["RegistryArn"]["Value"]
    assert output[0] == "UsesGaSurface"
    assert ":agent-registry:" in output[1]
    assert ":bedrock-agentcore:" in output[2]


def test_scheduled_distributor_is_serialized_and_dead_letters_failures():
    resources = _resources()
    function = resources["DistributorFunction"]["Properties"]
    queue = resources["DistributorDeadLetterQueue"]["Properties"]
    target = resources["DistributorSchedule"]["Properties"]["Targets"][0]

    assert function["ReservedConcurrentExecutions"] == 1
    assert function["DeadLetterConfig"]["TargetArn"] == "DistributorDeadLetterQueue.Arn"
    assert queue["MessageRetentionPeriod"] == 1209600
    assert target["DeadLetterConfig"]["Arn"] == "DistributorDeadLetterQueue.Arn"
    assert target["RetryPolicy"] == {
        "MaximumEventAgeInSeconds": 3600,
        "MaximumRetryAttempts": 2,
    }

    statements = _role_statements(resources["DistributorRole"])
    delivery = next(statement for statement in statements if statement.get("Sid") == "DeliverFailedAsyncInvocations")
    assert delivery["Action"] == "sqs:SendMessage"
    assert delivery["Resource"] == "DistributorDeadLetterQueue.Arn"

    queue_policy = resources["DistributorDeadLetterQueuePolicy"]["Properties"]["PolicyDocument"]["Statement"][0]
    assert queue_policy["Principal"] == {"Service": "events.amazonaws.com"}
    assert queue_policy["Condition"]["ArnEquals"]["aws:SourceArn"] == "DistributorSchedule.Arn"


def test_outputs_expose_deploy_contract():
    """deploy.py saves these outputs into the profile after a deploy."""
    outputs = _template()["Outputs"]
    for key in ("RegistryId", "ArtifactBucket", "DistributorFunctionArn", "PublisherRoleArn", "CuratorRoleArn"):
        assert key in outputs, f"missing output {key}"
