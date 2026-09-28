# ABOUTME: Template contract tests for deployment/infrastructure/memory-stack.yaml (E-G2, ADR-0016)
# ABOUTME: Covers params/conditions/mode pinning, IAM namespacePath scoping, and the actorId-not-a-tool-argument invariant

"""Contract tests for the AgentCore Memory stack template.

Mirrors the test_websearch_gateway.py template-contract style: the template
is parsed with the CFN-aware YAML loader and the security-relevant shapes are
asserted structurally (no AWS calls).
"""

from pathlib import Path

import yaml


def _memory_template() -> dict:
    from tests.test_cloudformation import CloudFormationLoader

    path = Path(__file__).parent.parent.parent / "deployment" / "infrastructure" / "memory-stack.yaml"
    with open(path, encoding="utf-8") as f:
        return yaml.load(f, Loader=CloudFormationLoader)  # nosec B506


# --- Parameters (ADR-0012 D4 contract) ---


def test_memory_scopes_default_disabled():
    """Content-bearing features are opt-in (A3/A7): both scopes default off."""
    params = _memory_template()["Parameters"]
    assert params["EnableUserMemory"]["Default"] == "false"
    assert params["EnableOrgMemory"]["Default"] == "false"


def test_memory_mode_defaults_to_extracted_only():
    params = _memory_template()["Parameters"]
    assert params["MemoryMode"]["Default"] == "extracted-only"
    assert params["MemoryMode"]["AllowedValues"] == ["extracted-only", "full"]


def test_raw_event_retention_bounds():
    """3-day floor is the service minimum; 365 the service maximum (R9 F10)."""
    params = _memory_template()["Parameters"]
    retention = params["RawEventRetentionDays"]
    assert retention["Default"] == 30
    assert retention["MinValue"] == 3
    assert retention["MaxValue"] == 365


def test_kms_key_optional_org_groups_empty_default():
    params = _memory_template()["Parameters"]
    assert params["KmsKeyArn"]["Default"] == ""
    assert params["OrgMemoryWriteGroups"]["Type"] == "CommaDelimitedList"
    assert params["OrgMemoryWriteGroups"]["Default"] == ""


def test_deploy_gate_defaults_to_log_only():
    """User memory cannot activate while AgentCore rejects identity forwarding."""
    params = _memory_template()["Parameters"]
    assert params["DeployGate"]["Default"] == "log-only"
    assert params["DeployGate"]["AllowedValues"] == ["log-only"]
    assert "cannot be activated" in _memory_template()["Metadata"]["SecurityGate"]["Note"]


def test_gateway_identifier_is_plain_string_parameter():
    """Second target from a separate stack: GatewayIdentifier is a plain String
    parameter (R9 F17), never a GetAtt into the websearch stack."""
    template = _memory_template()
    assert template["Parameters"]["GatewayIdentifier"]["Type"] == "String"
    target = template["Resources"]["MemoryTarget"]["Properties"]
    assert target["GatewayIdentifier"] == {"Ref": "GatewayIdentifier"}


def test_at_least_one_scope_rule():
    rules = _memory_template()["Rules"]
    assert "AtLeastOneMemoryScope" in rules


# --- Memory resource (replacement-on-update hazards, mode pinning) ---


def test_memory_name_derives_from_identity_pool_name_with_underscores():
    """Name is replacement-on-update: stable IdentityPoolName seed, hyphens
    converted to underscores (Name pattern forbids hyphens — R9 risk 7)."""
    memory = _memory_template()["Resources"]["ToolsMemory"]
    assert memory["Type"] == "AWS::BedrockAgentCore::Memory"
    name = memory["Properties"]["Name"]
    assert name == {"Fn::Join": ["_", {"Fn::Split": ["-", {"Fn::Sub": "${IdentityPoolName}-memory"}]}]}


def test_extracted_only_pins_event_expiry_to_service_minimum():
    """extracted-only pins EventExpiryDuration=3 (the service minimum); full
    mode uses the configured retention (ADR-0012 D4)."""
    memory = _memory_template()["Resources"]["ToolsMemory"]["Properties"]
    assert memory["EventExpiryDuration"] == {"Fn::If": ["IsExtractedOnly", 3, {"Ref": "RawEventRetentionDays"}]}


def test_encryption_key_set_from_day_one():
    """EncryptionKeyArn is replacement-on-update (R9 F10): always set —
    supplied CMK or the stack-managed one."""
    template = _memory_template()
    memory = template["Resources"]["ToolsMemory"]["Properties"]
    assert memory["EncryptionKeyArn"] == {
        "Fn::If": ["HasKmsKey", {"Ref": "KmsKeyArn"}, {"Fn::GetAtt": ["MemoryKmsKey", "Arn"]}]
    }
    assert template["Resources"]["MemoryKmsKey"]["Condition"] == "CreateKmsKey"


def test_strategies_are_scope_conditional_with_expected_namespaces():
    strategies = _memory_template()["Resources"]["ToolsMemory"]["Properties"]["MemoryStrategies"]
    rendered = []
    for entry in strategies:
        condition, value, _ = entry["Fn::If"]
        rendered.append((condition, value))
    user_entries = [v for c, v in rendered if c == "HasUserMemory"]
    org_entries = [v for c, v in rendered if c == "HasOrgMemory"]
    assert len(user_entries) == 2
    assert user_entries[0]["UserPreferenceMemoryStrategy"]["Namespaces"] == ["users/{actorId}/preferences"]
    assert user_entries[1]["SemanticMemoryStrategy"]["Namespaces"] == ["users/{actorId}/facts"]
    assert len(org_entries) == 1
    assert org_entries[0]["SemanticMemoryStrategy"]["Namespaces"] == ["org/knowledge"]


# --- IAM: fleet-level namespace scoping (R9 F12) ---


def _tools_role_statements() -> list:
    role = _memory_template()["Resources"]["MemoryToolsRole"]["Properties"]
    statements = role["Policies"][0]["PolicyDocument"]["Statement"]
    return [s["Fn::If"][1] for s in statements]  # unwrap scope conditionals


def test_tools_role_scopes_reads_by_namespace_path():
    """Retrieval actions carry bedrock-agentcore:namespacePath conditions:
    user reads confined to users/*, org reads to org/* (R9 F12)."""
    statements = _tools_role_statements()
    by_sid = {s["Sid"]: s for s in statements}
    user_reads = by_sid["UserNamespaceReads"]
    assert "bedrock-agentcore:RetrieveMemoryRecords" in user_reads["Action"]
    assert user_reads["Condition"] == {"StringLike": {"bedrock-agentcore:namespacePath": "users/*"}}
    org_reads = by_sid["OrgNamespaceReads"]
    assert org_reads["Condition"] == {"StringLike": {"bedrock-agentcore:namespacePath": "org/*"}}


def test_tools_role_write_actions_split_by_scope():
    statements = _tools_role_statements()
    by_sid = {s["Sid"]: s for s in statements}
    assert by_sid["UserEventWrites"]["Action"] == ["bedrock-agentcore:CreateEvent"]
    assert by_sid["OrgRecordWrites"]["Action"] == ["bedrock-agentcore:BatchCreateMemoryRecords"]
    # Everything is scoped to this memory's ARN — no wildcard resources.
    for statement in statements:
        assert statement["Resource"] == {"Fn::GetAtt": ["ToolsMemory", "MemoryArn"]}


def test_gateway_invoke_granted_via_resource_policy():
    """The websearch stack stays untouched (A1): invoke permission for its
    execution role is granted from THIS stack via a Lambda resource policy."""
    permission = _memory_template()["Resources"]["MemoryToolsInvokePermission"]["Properties"]
    assert permission["Action"] == "lambda:InvokeFunction"
    assert permission["Principal"] == {"Ref": "GatewayExecutionRoleArn"}


# --- THE security invariant: actorId is never a tool argument (ADR-0016) ---


def _inline_tools() -> list:
    target = _memory_template()["Resources"]["MemoryTarget"]["Properties"]
    payload = target["TargetConfiguration"]["Mcp"]["Lambda"]["ToolSchema"]["InlinePayload"]
    return [entry["Fn::If"][1] for entry in payload]  # unwrap scope conditionals


def test_no_tool_accepts_an_identity_argument():
    """R9 F8: Lambda targets receive no JWT claims, so a client-supplied
    actor/user id can never be trusted — no tool schema may declare one."""
    forbidden_fragments = ("actor", "user_id", "userid", "email", "sub")
    tools = _inline_tools()
    assert len(tools) == 4
    for tool in tools:
        properties = tool["InputSchema"].get("Properties", {})
        for prop_name in properties:
            assert not any(fragment in prop_name.lower() for fragment in forbidden_fragments), (
                f"tool {tool['Name']} declares identity-like argument '{prop_name}' — "
                "actorId must be server-derived (ADR-0016)"
            )
        for required in tool["InputSchema"].get("Required", []):
            assert "actor" not in required.lower()


def test_tool_names_and_scope_conditions():
    target = _memory_template()["Resources"]["MemoryTarget"]["Properties"]
    payload = target["TargetConfiguration"]["Mcp"]["Lambda"]["ToolSchema"]["InlinePayload"]
    by_name = {entry["Fn::If"][1]["Name"]: entry["Fn::If"][0] for entry in payload}
    assert by_name == {
        "memory_store": "HasUserMemory",
        "memory_retrieve": "HasUserMemory",
        "org_knowledge_search": "HasOrgMemory",
        "org_knowledge_add": "HasOrgMemory",
    }


def test_target_does_not_request_restricted_authorization_header():
    """AgentCore rejects Authorization in AllowedRequestHeaders (F-008)."""
    target = _memory_template()["Resources"]["MemoryTarget"]["Properties"]
    assert "MetadataConfiguration" not in target
    raw = (Path(__file__).parent.parent.parent / "deployment" / "infrastructure" / "memory-stack.yaml").read_text(
        encoding="utf-8"
    )
    assert "AllowedRequestHeaders:" not in raw


def test_cfn_lint_suppressions_present_for_schema_lag():
    """Same suppression pattern as the websearch Connector target: the
    BedrockAgentCore schemas lag in cfn-lint's bundled spec."""
    resources = _memory_template()["Resources"]
    for logical_id in ("ToolsMemory", "MemoryTarget"):
        ignore = resources[logical_id]["Metadata"]["cfn-lint"]["config"]["ignore_checks"]
        assert "E3002" in ignore


# --- Sweeper (extracted-only mode) + fail-visible alarms (A5) ---


def test_sweeper_resources_only_in_extracted_only_mode():
    resources = _memory_template()["Resources"]
    for logical_id in (
        "MemorySweeperRole",
        "MemorySweeperFunction",
        "MemorySweeperSchedule",
        "MemorySweeperSchedulePermission",
        "MemorySweeperErrorsAlarm",
        "MemorySweeperNotRunningAlarm",
    ):
        assert resources[logical_id]["Condition"] == "IsExtractedOnly", logical_id


def test_sweeper_dlq_uses_the_aws_managed_sqs_key():
    """Round-3 D9: KmsMasterKeyId with alias/aws/sqs, not MemoryKmsKey (that
    key is conditional on CreateKmsKey, the queue on IsExtractedOnly)."""
    queue = _memory_template()["Resources"]["MemorySweeperDLQ"]
    assert queue["Condition"] == "IsExtractedOnly"
    assert queue["Properties"]["KmsMasterKeyId"] == "alias/aws/sqs"
    assert "SqsManagedSseEnabled" not in queue["Properties"]
    assert "Metadata" not in queue


def test_sweeper_runs_daily_with_24h_window():
    resources = _memory_template()["Resources"]
    assert resources["MemorySweeperSchedule"]["Properties"]["ScheduleExpression"] == "rate(1 day)"
    env = resources["MemorySweeperFunction"]["Properties"]["Environment"]["Variables"]
    assert env["PURGE_AGE_HOURS"] == "24"


def test_three_alarms_and_fail_visible_sweeper_watchdog():
    """A5: three Lambda alarms; the sweeper watchdog breaches on missing data
    so a silently-dead schedule still raises."""
    resources = _memory_template()["Resources"]
    alarms = [k for k, v in resources.items() if v["Type"] == "AWS::CloudWatch::Alarm"]
    assert sorted(alarms) == [
        "MemorySweeperErrorsAlarm",
        "MemorySweeperNotRunningAlarm",
        "MemoryToolsErrorsAlarm",
    ]
    watchdog = resources["MemorySweeperNotRunningAlarm"]["Properties"]
    assert watchdog["TreatMissingData"] == "breaching"
    assert watchdog["ComparisonOperator"] == "LessThanThreshold"


def test_deploy_gate_and_scope_flags_reach_the_lambda():
    env = _memory_template()["Resources"]["MemoryToolsFunction"]["Properties"]["Environment"]["Variables"]
    assert env["DEPLOY_GATE"] == {"Ref": "DeployGate"}
    assert env["ENABLE_USER_MEMORY"] == {"Ref": "EnableUserMemory"}
    assert env["ENABLE_ORG_MEMORY"] == {"Ref": "EnableOrgMemory"}
    assert env["ORG_WRITE_GROUPS"] == {"Fn::Join": [",", {"Ref": "OrgMemoryWriteGroups"}]}
