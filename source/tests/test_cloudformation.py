# ABOUTME: Tests for CloudFormation template cross-region configuration
# ABOUTME: Validates IAM policies support cross-region inference properly

"""Tests for CloudFormation template configuration."""

import re
from pathlib import Path

import pytest
import yaml


# Custom YAML loader for CloudFormation templates
class CloudFormationLoader(yaml.SafeLoader):
    """Custom YAML loader that handles CloudFormation intrinsic functions."""

    pass


# Define constructors for CloudFormation intrinsic functions
def ref_constructor(loader, node):
    """Handle !Ref function."""
    return {"Ref": loader.construct_scalar(node)}


def getatt_constructor(loader, node):
    """Handle !GetAtt function."""
    if isinstance(node, yaml.SequenceNode):
        return {"Fn::GetAtt": loader.construct_sequence(node)}
    else:
        # Handle dot notation
        value = loader.construct_scalar(node)
        return {"Fn::GetAtt": value.split(".", 1)}


def sub_constructor(loader, node):
    """Handle !Sub function (scalar or sequence form)."""
    if node.id == "scalar":
        return {"Fn::Sub": loader.construct_scalar(node)}
    return {"Fn::Sub": loader.construct_sequence(node)}


def if_constructor(loader, node):
    """Handle !If function."""
    return {"Fn::If": loader.construct_sequence(node)}


def join_constructor(loader, node):
    """Handle !Join function."""
    return {"Fn::Join": loader.construct_sequence(node)}


def equals_constructor(loader, node):
    """Handle !Equals function."""
    return {"Fn::Equals": loader.construct_sequence(node)}


def or_constructor(loader, node):
    """Handle !Or function."""
    return {"Fn::Or": loader.construct_sequence(node)}


def and_constructor(loader, node):
    """Handle !And function."""
    return {"Fn::And": loader.construct_sequence(node)}


def not_constructor(loader, node):
    """Handle !Not function."""
    return {"Fn::Not": loader.construct_sequence(node)}


def condition_constructor(loader, node):
    """Handle !Condition function."""
    return {"Condition": loader.construct_scalar(node)}


def select_constructor(loader, node):
    """Handle !Select function."""
    return {"Fn::Select": loader.construct_sequence(node)}


def split_constructor(loader, node):
    """Handle !Split function."""
    return {"Fn::Split": loader.construct_sequence(node)}


def findinmap_constructor(loader, node):
    """Handle !FindInMap function."""
    return {"Fn::FindInMap": loader.construct_sequence(node)}


def getazs_constructor(loader, node):
    """Handle !GetAZs function."""
    return {"Fn::GetAZs": loader.construct_scalar(node)}


# Register the constructors
CloudFormationLoader.add_constructor("!Ref", ref_constructor)
CloudFormationLoader.add_constructor("!GetAtt", getatt_constructor)
CloudFormationLoader.add_constructor("!Sub", sub_constructor)
CloudFormationLoader.add_constructor("!If", if_constructor)
CloudFormationLoader.add_constructor("!Join", join_constructor)
CloudFormationLoader.add_constructor("!Equals", equals_constructor)
CloudFormationLoader.add_constructor("!Or", or_constructor)
CloudFormationLoader.add_constructor("!And", and_constructor)
CloudFormationLoader.add_constructor("!Not", not_constructor)
CloudFormationLoader.add_constructor("!Condition", condition_constructor)
CloudFormationLoader.add_constructor("!Select", select_constructor)
CloudFormationLoader.add_constructor("!Split", split_constructor)
CloudFormationLoader.add_constructor("!FindInMap", findinmap_constructor)
CloudFormationLoader.add_constructor("!GetAZs", getazs_constructor)


class TestCloudFormationCrossRegion:
    """Tests for CloudFormation template cross-region support."""

    def get_template(self):
        """Load the CloudFormation template."""
        template_path = (
            Path(__file__).parent.parent.parent / "deployment" / "infrastructure" / "cognito-identity-pool.yaml"
        )
        with open(template_path, encoding="utf-8") as f:
            return yaml.load(f, Loader=CloudFormationLoader)  # nosec B506

    def test_allowed_bedrock_regions_default(self):
        """Test that default AllowedBedrockRegions includes all US cross-region regions."""
        template = self.get_template()

        # Check parameters
        params = template.get("Parameters", {})
        assert "AllowedBedrockRegions" in params

        bedrock_regions_param = params["AllowedBedrockRegions"]
        assert bedrock_regions_param["Type"] == "CommaDelimitedList"

        # Check default value includes all US regions for cross-region
        default_regions = bedrock_regions_param.get("Default", "")
        assert "us-east-1" in default_regions
        assert "us-east-2" in default_regions
        assert "us-west-2" in default_regions

    def test_iam_policy_allows_cross_region_resources(self):
        """Test that IAM policy allows cross-region inference resources."""
        template = self.get_template()

        # Find the BedrockAccessPolicy
        resources = template.get("Resources", {})
        assert "BedrockAccessPolicy" in resources

        policy = resources["BedrockAccessPolicy"]
        assert policy["Type"] == "AWS::IAM::ManagedPolicy"

        # Check policy document
        policy_doc = policy["Properties"]["PolicyDocument"]
        statements = policy_doc["Statement"]

        # Find the AllowBedrockInvoke statement
        invoke_statement = None
        for stmt in statements:
            if stmt.get("Sid") == "AllowBedrockInvoke":
                invoke_statement = stmt
                break

        assert invoke_statement is not None

        # Check resources include cross-region patterns.
        # Resource may be a plain list or an Fn::If selecting between the
        # Anthropic-scoped and unrestricted lists (RestrictToAnthropicModels).
        resources_allowed = invoke_statement["Resource"]
        if isinstance(resources_allowed, dict) and "Fn::If" in resources_allowed:
            branches = resources_allowed["Fn::If"][1:]
        else:
            branches = [resources_allowed]

        for branch in branches:
            assert isinstance(branch, list)

            # Extract actual resource strings from Fn::Sub or plain strings
            resource_strings = []
            for r in branch:
                if isinstance(r, dict) and "Fn::Sub" in r:
                    resource_strings.append(r["Fn::Sub"])
                elif isinstance(r, str):
                    resource_strings.append(r)

            # Should allow foundation models (cross-region)
            assert any("foundation-model" in r for r in resource_strings)

            # Should allow inference profiles
            assert any("inference-profile" in r for r in resource_strings)

            # Check ARN patterns for cross-region (double colon between region and account)
            assert any("*::foundation-model" in r for r in resource_strings)

    def test_iam_policy_has_region_condition(self):
        """Test that IAM policy has region condition for security."""
        template = self.get_template()

        resources = template.get("Resources", {})
        policy = resources["BedrockAccessPolicy"]
        policy_doc = policy["Properties"]["PolicyDocument"]
        statements = policy_doc["Statement"]

        # Find the AllowBedrockInvoke statement
        for stmt in statements:
            if stmt.get("Sid") == "AllowBedrockInvoke":
                # Should have a condition
                assert "Condition" in stmt

                condition = stmt["Condition"]
                assert "StringEquals" in condition

                # Should check aws:RequestedRegion
                string_equals = condition["StringEquals"]
                assert "aws:RequestedRegion" in string_equals

                # The value should reference the AllowedBedrockRegions parameter
                region_ref = string_equals["aws:RequestedRegion"]
                # Check if it's a Ref to AllowedBedrockRegions
                assert isinstance(region_ref, dict)
                assert "Ref" in region_ref
                assert region_ref["Ref"] == "AllowedBedrockRegions"
                break

    def test_bedrock_access_role_configuration(self):
        """Test that the BedrockAccessRole is properly configured."""
        template = self.get_template()

        resources = template.get("Resources", {})
        assert "BedrockAccessRole" in resources

        role = resources["BedrockAccessRole"]
        assert role["Type"] == "AWS::IAM::Role"

        # Check it references the BedrockAccessPolicy
        policy_arns = role["Properties"]["ManagedPolicyArns"]
        # Look for the reference to BedrockAccessPolicy
        found_policy_ref = False
        for arn in policy_arns:
            if isinstance(arn, dict) and "Ref" in arn and arn["Ref"] == "BedrockAccessPolicy":
                found_policy_ref = True
                break
        assert found_policy_ref, "BedrockAccessPolicy not referenced in ManagedPolicyArns"

        # Check assume role policy for Cognito
        assume_policy = role["Properties"]["AssumeRolePolicyDocument"]
        statements = assume_policy["Statement"]

        assert len(statements) > 0
        assume_stmt = statements[0]

        # Should allow Cognito Identity to assume
        # The federated principal may be a string or a conditional (Fn::If) for GovCloud
        federated = assume_stmt["Principal"]["Federated"]
        if isinstance(federated, dict) and "Fn::If" in federated:
            # It's a conditional - verify it includes cognito-identity endpoints
            assert "cognito-identity" in str(federated)
        else:
            # It's a plain string
            assert federated == "cognito-identity.amazonaws.com"

        assert "sts:AssumeRoleWithWebIdentity" in assume_stmt["Action"]

    def test_template_description_mentions_cross_region(self):
        """Test that template description or comments mention cross-region inference."""
        template = self.get_template()

        # Check if Parameters description mentions cross-region
        params = template.get("Parameters", {})
        bedrock_param = params.get("AllowedBedrockRegions", {})
        description = bedrock_param.get("Description", "")

        # Should mention cross-region or multiple regions
        assert "cross-region" in description.lower() or "regions" in description.lower()

    def test_outputs_include_identity_pool(self):
        """Test that outputs include the Identity Pool ID."""
        template = self.get_template()

        outputs = template.get("Outputs", {})
        assert "IdentityPoolId" in outputs

        pool_output = outputs["IdentityPoolId"]
        # Check if Value is a Ref to BedrockIdentityPool
        value = pool_output["Value"]
        assert isinstance(value, dict)
        assert "Ref" in value
        assert value["Ref"] == "BedrockIdentityPool"

    def test_anonymous_credentials_are_disabled(self):
        """The legacy pool must require an authenticated provider login."""
        template = self.get_template()
        pool = template["Resources"]["BedrockIdentityPool"]

        assert pool["Properties"]["AllowUnauthenticatedIdentities"] is False

    def test_unauthenticated_role_has_no_permissions(self):
        """The retained update-compatible role relies on IAM's implicit deny."""
        template = self.get_template()
        resources = template["Resources"]
        role_properties = resources["UnauthenticatedRole"]["Properties"]

        assert "Policies" not in role_properties
        assert "ManagedPolicyArns" not in role_properties

        roles = resources["IdentityPoolRoleAttachment"]["Properties"]["Roles"]
        assert roles["authenticated"] == {"Fn::GetAtt": ["BedrockAccessRole", "Arn"]}
        assert roles["unauthenticated"] == {"Fn::GetAtt": ["UnauthenticatedRole", "Arn"]}


# Okta thumbprint that used to be hardcoded in bedrock-auth-okta.yaml — must NOT appear in the generic template
OKTA_HARDCODED_THUMBPRINT = "9e99a48a9960b14926bb7f3b02e22da2b0ab7280"

# IAM OIDC provider thumbprints are 40 hex chars. No auth template may carry a literal one:
# the IAM OIDC provider's ThumbprintList is optional (IAM retrieves the CA thumbprint itself)
# and hardcoded values go stale when a provider rotates CAs.
AUTH_TEMPLATES_WITHOUT_LITERAL_THUMBPRINTS = [
    "bedrock-auth-okta.yaml",
    "bedrock-auth-azure.yaml",
    "bedrock-auth-auth0.yaml",
    "bedrock-auth-google.yaml",
    "bedrock-auth-generic.yaml",
    "bedrock-auth-cognito-pool.yaml",
    "cognito-identity-pool.yaml",
]
INFRA_DIR = Path(__file__).parent.parent.parent / "deployment" / "infrastructure"


def _evaluate_condition(node, conditions, params):
    """Minimal evaluator for the intrinsic subset the auth templates use in Conditions."""
    if isinstance(node, dict) and len(node) == 1:
        (fn, arg), *_ = node.items()
        if fn == "Ref":
            return params[arg]
        if fn == "Condition":
            return _evaluate_condition(conditions[arg], conditions, params)
        if fn == "Fn::Join":
            delimiter, values = arg
            values = _evaluate_condition(values, conditions, params)
            return delimiter.join(values)
        if fn == "Fn::Equals":
            left, right = (_evaluate_condition(a, conditions, params) for a in arg)
            return left == right
        if fn == "Fn::Not":
            return not _evaluate_condition(arg[0], conditions, params)
        if fn == "Fn::Or":
            return any(_evaluate_condition(a, conditions, params) for a in arg)
        if fn == "Fn::And":
            return all(_evaluate_condition(a, conditions, params) for a in arg)
    return node


def _resolve_if(node, conditions, params):
    """Resolve a top-level Fn::If the way CloudFormation would for the given parameters."""
    condition_name, when_true, when_false = node["Fn::If"]
    chosen = when_true if _evaluate_condition(conditions[condition_name], conditions, params) else when_false
    if chosen == {"Ref": "AWS::NoValue"}:
        return None
    if isinstance(chosen, dict) and list(chosen) == ["Ref"]:
        return params[chosen["Ref"]]
    return chosen


class TestBedrockAuthGenericTemplate:
    """Tests for bedrock-auth-generic.yaml — covers PingFederate/Keycloak/ForgeRock/etc.

    The template was added to fix a bug where choosing 'Okta (or generic OIDC)' for a
    non-Okta IdP silently applied the Okta template. The generic template must:
      - take the OIDC issuer URL and client ID as parameters, plus an OPTIONAL thumbprint list
      - NOT hardcode the Okta thumbprint
      - NOT contain Okta-specific strings in tags/descriptions
      - emit the same set of outputs as the Okta template (downstream stacks rely on these)
    """

    def get_template(self):
        template_path = (
            Path(__file__).parent.parent.parent / "deployment" / "infrastructure" / "bedrock-auth-generic.yaml"
        )
        with open(template_path, encoding="utf-8") as f:
            return yaml.load(f, Loader=CloudFormationLoader)  # nosec B506

    def test_template_loads(self):
        """Template must parse as valid CloudFormation YAML."""
        template = self.get_template()
        assert template["AWSTemplateFormatVersion"] == "2010-09-09"
        assert "Parameters" in template
        assert "Resources" in template
        assert "Outputs" in template

    def test_required_oidc_parameters(self):
        """Must accept issuer URL, client ID, and an optional thumbprint list as parameters."""
        params = self.get_template()["Parameters"]

        assert "OidcIssuerUrl" in params
        assert "OidcClientId" in params
        assert "OidcThumbprintList" in params
        # ThumbprintList must be a CommaDelimitedList — IAM OIDC supports several thumbprints
        assert params["OidcThumbprintList"]["Type"] == "CommaDelimitedList"
        # ... and optional: an empty default lets IAM retrieve the CA thumbprint itself.
        # Existing deployments that never set it must keep updating without a new required param.
        assert params["OidcThumbprintList"]["Default"] == ""
        description = params["OidcThumbprintList"]["Description"]
        assert "private CA" in description
        assert "id_roles_providers_create_oidc_verify-thumbprint.html" in description
        assert "openssl" not in description.lower()
        # Issuer URL pattern must require https://
        assert params["OidcIssuerUrl"]["AllowedPattern"].startswith("^https://")

    def test_no_okta_specific_parameters(self):
        """Must not carry over OktaDomain/OktaClientId from the okta template."""
        params = self.get_template()["Parameters"]
        assert "OktaDomain" not in params
        assert "OktaClientId" not in params

    def test_oidc_provider_resource_uses_parameter_thumbprint(self):
        """OIDC provider must reference the parameter through a condition, not hardcode a thumbprint."""
        resources = self.get_template()["Resources"]
        assert "OidcProvider" in resources
        oidc_provider = resources["OidcProvider"]
        assert oidc_provider["Type"] == "AWS::IAM::OIDCProvider"

        thumbprint_list = oidc_provider["Properties"]["ThumbprintList"]
        # Must be !If [HasOidcThumbprints, !Ref OidcThumbprintList, !Ref AWS::NoValue]
        assert isinstance(thumbprint_list, dict), f"ThumbprintList must be an !If, got literal: {thumbprint_list}"
        assert thumbprint_list == {
            "Fn::If": ["HasOidcThumbprints", {"Ref": "OidcThumbprintList"}, {"Ref": "AWS::NoValue"}]
        }

    def test_has_oidc_thumbprints_condition_checks_joined_list(self):
        """An empty CommaDelimitedList cannot be compared to '' directly; it must be joined first."""
        conditions = self.get_template()["Conditions"]
        assert conditions["HasOidcThumbprints"] == {
            "Fn::Not": [{"Fn::Equals": [{"Fn::Join": ["", {"Ref": "OidcThumbprintList"}]}, ""]}]
        }

    @pytest.mark.parametrize(
        ("param_value", "expected"),
        [
            ([""], None),  # Default: '' -> CommaDelimitedList [''] -> property omitted (AWS::NoValue)
            (["9e99a48a9960b14926bb7f3b02e22da2b0ab7280"], ["9e99a48a9960b14926bb7f3b02e22da2b0ab7280"]),
            (
                ["9e99a48a9960b14926bb7f3b02e22da2b0ab7280", "60b5e7d8fbad8a16a1caf68d01354c20bb1f8620"],
                ["9e99a48a9960b14926bb7f3b02e22da2b0ab7280", "60b5e7d8fbad8a16a1caf68d01354c20bb1f8620"],
            ),
        ],
    )
    def test_thumbprint_list_resolves_to_novalue_when_empty_and_to_list_when_set(self, param_value, expected):
        """Regression: with the parameter at its empty default the property must disappear so IAM
        retrieves the CA thumbprint; when set (older profiles) the configured list must be sent."""
        template = self.get_template()
        thumbprint_list = template["Resources"]["OidcProvider"]["Properties"]["ThumbprintList"]
        resolved = _resolve_if(thumbprint_list, template["Conditions"], {"OidcThumbprintList": param_value})
        assert resolved == expected

    def test_no_hardcoded_okta_thumbprint_anywhere(self):
        """The Okta-specific thumbprint constant must not appear anywhere in the template."""
        template = self.get_template()
        # Stringify the entire template to catch the thumbprint regardless of where it sits
        import json

        serialized = json.dumps(template, default=str)
        assert OKTA_HARDCODED_THUMBPRINT not in serialized, (
            f"Okta-specific thumbprint {OKTA_HARDCODED_THUMBPRINT} leaked into generic template"
        )

    def test_no_okta_substring_in_tags_or_descriptions(self):
        """Tags, descriptions, and resource names must not advertise Okta."""
        import json

        template = self.get_template()
        serialized = json.dumps(template, default=str).lower()
        # 'okta' should not appear anywhere — this template is provider-agnostic
        assert "okta" not in serialized, "Generic template still contains 'okta' references"

    def test_outputs_match_okta_template_contract(self):
        """Downstream stacks (monitoring, packaging) consume these outputs by name."""
        outputs = self.get_template()["Outputs"]
        for required_output in (
            "FederationType",
            "OIDCProviderArn",
            "FederatedRoleArn",
            "DirectSTSRoleArn",
            "BedrockRoleArn",
            "IdentityPoolId",
            "BedrockPolicyArn",
            "ConfigurationJson",
        ):
            assert required_output in outputs, f"Missing output: {required_output}"

    def test_configuration_json_marks_provider_type_as_generic(self):
        """The ConfigurationJson output must declare provider_type=generic so downstream
        consumers don't misclassify the deployment."""
        outputs = self.get_template()["Outputs"]
        config_json = outputs["ConfigurationJson"]["Value"]
        # Value is a !If [cond, direct-config-string, cognito-config-string].
        # Both branches are Fn::Sub strings — verify both contain provider_type=generic.
        if_branches = config_json["Fn::If"]
        assert len(if_branches) == 3, "Expected !If [condition, direct, cognito]"
        for branch in if_branches[1:]:
            assert "Fn::Sub" in branch
            sub_string = branch["Fn::Sub"]
            assert '"provider_type": "generic"' in sub_string, f"Expected provider_type=generic in: {sub_string!r}"

    def test_supports_both_federation_modes(self):
        """Template must support both direct STS and Cognito Identity Pool federation."""
        template = self.get_template()

        params = template["Parameters"]
        assert params["FederationType"]["AllowedValues"] == ["direct", "cognito"]

        # Both conditions must exist
        conditions = template["Conditions"]
        assert "UseDirectIAM" in conditions
        assert "UseCognitoIdentity" in conditions

        # Both role variants must exist
        resources = template["Resources"]
        assert "DirectIAMRole" in resources
        assert "CognitoAuthenticatedRole" in resources

    def test_govcloud_partition_aware(self):
        """Cognito service principals must select the GovCloud variant when deployed there."""
        template = self.get_template()
        conditions = template["Conditions"]
        assert "IsGovCloudWest" in conditions
        assert "IsGovCloudEast" in conditions

        # The Cognito role's principal should reference these (verified by string search —
        # the nested !If chain is awkward to traverse but the string presence is sufficient)
        import json

        cognito_role = template["Resources"]["CognitoAuthenticatedRole"]
        serialized = json.dumps(cognito_role, default=str)
        assert "cognito-identity-us-gov.amazonaws.com" in serialized
        assert "cognito-identity.us-gov-east-1.amazonaws.com" in serialized

    def test_bedrock_policy_uses_partition_pseudoparameter(self):
        """ARN construction must use ${AWS::Partition} for multi-partition support."""
        template = self.get_template()
        policy = template["Resources"]["BedrockAccessPolicy"]
        policy_doc = policy["Properties"]["PolicyDocument"]

        # Find any Resource entries — they should contain ${AWS::Partition}, not literal "aws".
        # Resource may be wrapped in Fn::If (RestrictToAnthropicModels branches).
        partition_found = False
        for stmt in policy_doc["Statement"]:
            if "Resource" in stmt:
                resource = stmt["Resource"]
                if isinstance(resource, dict) and "Fn::If" in resource:
                    resources = [r for branch in resource["Fn::If"][1:] for r in branch]
                elif isinstance(resource, list):
                    resources = resource
                else:
                    resources = [resource]
                for r in resources:
                    if isinstance(r, dict) and "Fn::Sub" in r and "${AWS::Partition}" in r["Fn::Sub"]:
                        partition_found = True
                        break
        assert partition_found, "Bedrock ARNs must use ${AWS::Partition} for GovCloud support"


# --- Wave 3 CFN security fixes (REVIEW E-R3, fix-this-wave F-1..F-6) ---

INFRA_DIR = Path(__file__).parent.parent.parent / "deployment" / "infrastructure"


def load_infra_template(name):
    """Load a template from deployment/infrastructure by file name."""
    with open(INFRA_DIR / name, encoding="utf-8") as f:
        return yaml.load(f, Loader=CloudFormationLoader)  # nosec B506


class TestAuthTemplatesCarryNoLiteralThumbprints:
    """No OIDC auth template may hardcode an IAM OIDC provider thumbprint.

    ThumbprintList is optional on AWS::IAM::OIDCProvider — when omitted, IAM retrieves the
    top intermediate CA thumbprint and validates the JWKS TLS certificate against its trusted
    root CA library. Hardcoded values go stale when the provider rotates CAs, and the Google
    template's all-'a' placeholder was never a real thumbprint. The raw file text is scanned
    (not the parsed YAML) so comments cannot smuggle one back in either.
    """

    @pytest.mark.parametrize("template_name", AUTH_TEMPLATES_WITHOUT_LITERAL_THUMBPRINTS)
    def test_no_literal_40_hex_thumbprint(self, template_name):
        text = (INFRA_DIR / template_name).read_text(encoding="utf-8")
        matches = re.findall(r"\b[0-9a-fA-F]{40}\b", text)
        assert matches == [], f"{template_name} contains literal thumbprint(s): {matches}"

    @pytest.mark.parametrize("template_name", AUTH_TEMPLATES_WITHOUT_LITERAL_THUMBPRINTS)
    def test_oidc_providers_gate_thumbprint_list_behind_parameter(self, template_name):
        """Every OIDC provider must be `!If [Has*Thumbprints, !Ref *ThumbprintList, AWS::NoValue]`.

        Omitting the property outright is not enough: IAM rejects removing ThumbprintList
        from an EXISTING provider (UpdateOpenIDConnectProviderThumbprint with null -> 400
        "Member must not be null", verified live), so upgrades of stacks created with
        thumbprints need a parameter to carry the current list through.
        """
        with open(INFRA_DIR / template_name, encoding="utf-8") as f:
            template = yaml.load(f, Loader=CloudFormationLoader)  # nosec B506
        providers = {
            name: res for name, res in template["Resources"].items() if res.get("Type") == "AWS::IAM::OIDCProvider"
        }
        assert providers, f"{template_name} declares no AWS::IAM::OIDCProvider"
        for name, provider in providers.items():
            thumbprint_list = provider["Properties"].get("ThumbprintList")
            assert isinstance(thumbprint_list, dict) and "Fn::If" in thumbprint_list, (
                f"{template_name}/{name}: ThumbprintList must be an !If, got {thumbprint_list!r}"
            )
            condition_name, when_true, when_false = thumbprint_list["Fn::If"]
            param_name = when_true["Ref"]
            assert when_false == {"Ref": "AWS::NoValue"}, f"{template_name}/{name}"
            param = template["Parameters"][param_name]
            assert param["Type"] == "CommaDelimitedList" and param["Default"] == "", f"{template_name}/{param_name}"
            assert "get-open-id-connect-provider" in param["Description"], f"{template_name}/{param_name}"
            assert template["Conditions"][condition_name] == {
                "Fn::Not": [{"Fn::Equals": [{"Fn::Join": ["", {"Ref": param_name}]}, ""]}]
            }, f"{template_name}/{condition_name}"
            # Behaviour: empty -> property omitted (new stack); set -> current list carried through.
            conditions = template["Conditions"]
            assert _resolve_if(thumbprint_list, conditions, {param_name: [""]}) is None
            preserved = ["08745487e891c19e3078c1f2a07e452950ef36f6"]
            assert _resolve_if(thumbprint_list, conditions, {param_name: preserved}) == preserved


class TestOtelCollectorPort80Redirect:
    """F-1: the :80 listener must never forward to the collector when HTTPS+JWT
    is enabled — a plaintext forward bypasses JWT validation and lets anyone
    inject OTLP telemetry with arbitrary attribution headers."""

    def get_listener_actions(self):
        template = load_infra_template("otel-collector.yaml")
        listener = template["Resources"]["HTTPListener"]
        assert listener["Type"] == "AWS::ElasticLoadBalancingV2::Listener"
        return listener["Properties"]["DefaultActions"]

    def test_default_actions_switch_on_enable_https(self):
        actions = self.get_listener_actions()
        assert isinstance(actions, dict) and "Fn::If" in actions, (
            "HTTPListener DefaultActions must be conditional on EnableHttps"
        )
        assert actions["Fn::If"][0] == "EnableHttps"

    def test_https_branch_redirects_and_does_not_forward(self):
        actions = self.get_listener_actions()
        https_branch = actions["Fn::If"][1]
        assert len(https_branch) == 1
        action = https_branch[0]
        assert action["Type"] == "redirect"
        assert "TargetGroupArn" not in action, "HTTPS branch must not forward to the collector"
        redirect = action["RedirectConfig"]
        assert redirect["Protocol"] == "HTTPS"
        assert str(redirect["Port"]) == "443"
        assert redirect["StatusCode"] == "HTTP_301"

    def test_explicit_insecure_branch_still_forwards(self):
        """The internal local-dev opt-in retains the legacy HTTP action."""
        actions = self.get_listener_actions()
        http_branch = actions["Fn::If"][2]
        assert len(http_branch) == 1
        action = http_branch[0]
        assert action["Type"] == "forward"
        assert action["TargetGroupArn"] == {"Ref": "HTTPTargetGroup"}

    def test_http_service_requires_explicit_insecure_condition(self):
        template = load_infra_template("otel-collector.yaml")
        assert template["Resources"]["ECSServiceHTTP"]["Condition"] == "EnableInsecureHttpIngress"


USAGE_NAMESPACES = ["GIP/Bedrock/Usage", "AWS/Bedrock"]


def _find_statements(policy_statements):
    """Unwrap conditional (Fn::If) statements into plain dicts."""
    result = []
    for stmt in policy_statements:
        if isinstance(stmt, dict) and "Fn::If" in stmt:
            for branch in stmt["Fn::If"][1:]:
                if isinstance(branch, dict):
                    result.append(branch)
        elif isinstance(stmt, dict):
            result.append(stmt)
    return result


class TestEndUserPutMetricDataNamespaceScoped:
    """F-2: every end-user cloudwatch:PutMetricData grant must carry the
    cloudwatch:namespace condition (same pattern as bedrock-auth-cognito-pool
    AllowCloudWatchMetrics) so federated developers cannot poison arbitrary
    namespaces (alarm/dashboard poisoning)."""

    def test_identity_pool_otlp_grant_is_namespace_scoped(self):
        template = load_infra_template("cognito-identity-pool.yaml")
        statements = template["Resources"]["BedrockAccessPolicy"]["Properties"]["PolicyDocument"]["Statement"]
        otlp = [s for s in _find_statements(statements) if s.get("Sid") == "AllowCloudWatchOTLP"]
        assert len(otlp) == 1, "expected exactly one AllowCloudWatchOTLP statement"
        string_equals = otlp[0]["Condition"]["StringEquals"]
        assert string_equals.get("cloudwatch:namespace") == USAGE_NAMESPACES
        # The pre-existing region pin must survive the namespace addition
        assert string_equals.get("aws:RequestedRegion") == {"Ref": "AllowedBedrockRegions"}

    def get_idc_statements(self):
        template = load_infra_template("bedrock-auth-idc.yaml")
        policies = template["Resources"]["BedrockIDCRole"]["Properties"]["Policies"]
        # Policies is !If [MonitoringEnabled, [policy], []]
        assert isinstance(policies, dict) and "Fn::If" in policies
        policy_list = policies["Fn::If"][1]
        assert policy_list[0]["PolicyName"] == "CloudWatchAccess"
        return policy_list[0]["PolicyDocument"]["Statement"]

    def test_idc_put_metric_data_is_namespace_scoped(self):
        statements = self.get_idc_statements()
        writes = [s for s in statements if "cloudwatch:PutMetricData" in s.get("Action", [])]
        assert len(writes) == 1, "expected exactly one PutMetricData statement"
        write = writes[0]
        assert write["Action"] == ["cloudwatch:PutMetricData"], (
            "PutMetricData must not share a statement with unconditioned actions"
        )
        assert write["Condition"]["StringEquals"]["cloudwatch:namespace"] == USAGE_NAMESPACES

    def test_idc_read_actions_remain_available(self):
        """ListMetrics/GetMetricStatistics must keep working (ListMetrics without
        a Namespace filter would be denied by the namespace condition)."""
        statements = self.get_idc_statements()
        reads = [s for s in statements if "cloudwatch:ListMetrics" in s.get("Action", [])]
        assert len(reads) == 1
        assert "cloudwatch:GetMetricStatistics" in reads[0]["Action"]
        assert "cloudwatch:PutMetricData" not in reads[0]["Action"]


class TestSecretStoreRoleScoped:
    """F-3: SecretStoreRole must only touch the one secret it manages — an
    UpdateSecret grant on secret:* lets a compromised custom-resource Lambda
    overwrite every other stack's secrets."""

    def test_secretsmanager_grant_scoped_to_managed_secret(self):
        template = load_infra_template("cognito-user-pool-setup.yaml")
        role = template["Resources"]["SecretStoreRole"]
        statements = role["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
        secret_stmts = [s for s in statements if any(str(a).startswith("secretsmanager:") for a in s.get("Action", []))]
        assert len(secret_stmts) == 1
        for resource in secret_stmts[0]["Resource"]:
            arn = resource["Fn::Sub"] if isinstance(resource, dict) else resource
            assert ":secret:${AWS::StackName}-distribution-web-client-secret" in arn, (
                f"secretsmanager grant not scoped to the managed secret: {arn}"
            )
            assert not arn.endswith(":secret:*"), f"wildcard secret grant: {arn}"


class TestAnalyticsPipelineIamHardening:
    """F-4 + F-5: analytics-pipeline roles — no unused wildcard logs grant, and
    service trust policies carry confused-deputy conditions (the pattern from
    quota-metering BedrockLoggingRole / bedrock-agentcore-gateway)."""

    def get_template(self):
        return load_infra_template("analytics-pipeline.yaml")

    def test_firehose_role_has_no_logs_grant(self):
        """F-4: logs:PutLogEvents on '*' was unused (no CloudWatchLoggingOptions
        on the delivery stream) and enabled account-wide log poisoning."""
        role = self.get_template()["Resources"]["FirehoseDeliveryRole"]
        for policy in role["Properties"]["Policies"]:
            for stmt in policy["PolicyDocument"]["Statement"]:
                actions = stmt.get("Action", [])
                if not isinstance(actions, list):
                    actions = [actions]
                logs_actions = [a for a in actions if str(a).startswith("logs:")]
                assert not logs_actions, f"unexpected logs grant on FirehoseDeliveryRole: {logs_actions}"

    def test_firehose_trust_has_source_account_condition(self):
        role = self.get_template()["Resources"]["FirehoseDeliveryRole"]
        stmt = role["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]
        assert stmt["Principal"]["Service"] == "firehose.amazonaws.com"
        assert stmt["Condition"]["StringEquals"]["aws:SourceAccount"] == {"Ref": "AWS::AccountId"}

    def test_logs_trust_has_source_account_and_arn_conditions(self):
        role = self.get_template()["Resources"]["LogsRole"]
        stmt = role["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]
        condition = stmt["Condition"]
        assert condition["StringEquals"]["aws:SourceAccount"] == {"Ref": "AWS::AccountId"}
        source_arn = condition["ArnLike"]["aws:SourceArn"]
        assert source_arn["Fn::Sub"] == "arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:*"


class TestAnalyticsPartitionProjection:
    """The analytics table must not stop discovering current-year partitions."""

    def test_year_projection_advances_with_current_date(self):
        template = load_infra_template("analytics-pipeline.yaml")
        parameters = template["Resources"]["GlueTable"]["Properties"]["TableInput"]["Parameters"]

        assert parameters["projection.year.type"] == "date"
        assert parameters["projection.year.range"] == "2024,NOW"
        assert parameters["projection.year.format"] == "yyyy"
        assert parameters["projection.year.interval.unit"] == "YEARS"


class TestCodeBuildRoleLogsScoped:
    """F-6: CodeBuildServiceRole must not hold CloudWatchLogsFullAccess (logs
    admin on every log group in the account, on a role that executes build
    scripts from an S3 source.zip). Only scoped create/put on its own three
    build log groups is allowed."""

    def get_role(self):
        template = load_infra_template("codebuild-windows.yaml")
        return template["Resources"]["CodeBuildServiceRole"]

    def test_no_logs_full_access_managed_policy(self):
        role = self.get_role()
        for arn in role["Properties"].get("ManagedPolicyArns", []):
            serialized = arn["Fn::Sub"] if isinstance(arn, dict) else str(arn)
            assert "CloudWatchLogsFullAccess" not in serialized

    def test_inline_logs_policy_scoped_to_build_log_groups(self):
        role = self.get_role()
        policies = {p["PolicyName"]: p for p in role["Properties"]["Policies"]}
        assert "CodeBuildLogsPolicy" in policies
        statements = policies["CodeBuildLogsPolicy"]["PolicyDocument"]["Statement"]
        assert len(statements) == 1
        stmt = statements[0]
        assert sorted(stmt["Action"]) == [
            "logs:CreateLogGroup",
            "logs:CreateLogStream",
            "logs:PutLogEvents",
        ]
        resources = stmt["Resource"]
        assert isinstance(resources, list) and resources, "logs grant must be resource-scoped"
        log_group_refs = {"CodeBuildLogGroup", "LinuxX64BuildLogGroup", "LinuxArm64BuildLogGroup"}
        for resource in resources:
            if isinstance(resource, dict) and "Fn::GetAtt" in resource:
                assert resource["Fn::GetAtt"][0] in log_group_refs
            else:
                arn = resource["Fn::Sub"] if isinstance(resource, dict) else str(resource)
                assert ":log-group:/aws/codebuild/${ProjectNamePrefix}-" in arn, (
                    f"logs grant not scoped to build log groups: {arn}"
                )


class TestCodeBuildArtifactEncryption:
    """Round-3 R32-R34 (CKV_AWS_78 / CODEBUILD_ENCRYPTION_KEY_RULE): artifacts
    are written with SSE-KMS under the AWS-managed aws/s3 key (CodeBuild's own
    default, made explicit). EncryptionDisabled must stay absent; its key
    policy covers every principal in the account through S3, so no kms:*
    grants are needed on the service role or the downloaders."""

    def test_every_project_encrypts_artifacts_with_the_aws_managed_s3_key(self):
        template = load_infra_template("codebuild-windows.yaml")
        projects = {k: v for k, v in template["Resources"].items() if v["Type"] == "AWS::CodeBuild::Project"}
        assert set(projects) == {"WindowsBuildProject", "LinuxX64BuildProject", "LinuxArm64BuildProject"}
        for name, project in projects.items():
            props = project["Properties"]
            assert props["Artifacts"]["Type"] == "S3", name
            assert "EncryptionDisabled" not in props["Artifacts"], name
            assert props["EncryptionKey"] == {
                "Fn::Sub": "arn:${AWS::Partition}:kms:${AWS::Region}:${AWS::AccountId}:alias/aws/s3"
            }, name
            assert "Metadata" not in project, f"{name}: CKV_AWS_78 skip / guard suppression must be gone"

    def test_service_role_needs_no_kms_grant(self):
        role = load_infra_template("codebuild-windows.yaml")["Resources"]["CodeBuildServiceRole"]
        actions = [
            a
            for p in role["Properties"]["Policies"]
            for s in p["PolicyDocument"]["Statement"]
            for a in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])
        ]
        assert not [a for a in actions if a.startswith("kms:")]


class TestLogBucketDeliveryPolicies:
    """Round-3 D8 content fix: three server-access-log destination buckets had
    no bucket policy, so with Object Ownership enforced (S3 default since
    2023) log delivery was silently dropped. Each now grants
    logging.s3.amazonaws.com s3:PutObject on the source bucket's prefix,
    scoped by aws:SourceAccount + aws:SourceArn (AWS's current example), and
    carries the TLS-only deny so S3_BUCKET_SSL_REQUESTS_ONLY is not regressed."""

    CASES = {
        "codebuild-windows.yaml": ("LoggingBucket", "BuildBucket", "build-logs/"),
        "distribution.yaml": ("LoggingBucket", "DistributionBucket", "package-downloads/"),
        "presigned-s3-distribution.yaml": ("LoggingBucket", "DistributionBucket", "package-downloads/"),
    }

    def test_log_delivery_grant_matches_the_source_bucket_logging_configuration(self):
        for template_name, (log_bucket, source_bucket, prefix) in self.CASES.items():
            resources = load_infra_template(template_name)["Resources"]
            logging_conf = resources[source_bucket]["Properties"]["LoggingConfiguration"]
            assert logging_conf == {"DestinationBucketName": {"Ref": log_bucket}, "LogFilePrefix": prefix}, (
                template_name
            )

            policy = resources[f"{log_bucket}Policy"]
            assert policy["Type"] == "AWS::S3::BucketPolicy"
            assert "Condition" not in policy
            assert policy["Properties"]["Bucket"] == {"Ref": log_bucket}
            statements = {s["Sid"]: s for s in policy["Properties"]["PolicyDocument"]["Statement"]}
            assert set(statements) == {"DenyInsecureTransport", "S3ServerAccessLogsPolicy"}, template_name

            grant = statements["S3ServerAccessLogsPolicy"]
            assert grant["Effect"] == "Allow"
            assert grant["Principal"] == {"Service": "logging.s3.amazonaws.com"}
            assert grant["Action"] == "s3:PutObject"
            assert grant["Resource"] == {"Fn::Sub": f"${{{log_bucket}.Arn}}/{prefix}*"}
            assert grant["Condition"] == {
                "StringEquals": {"aws:SourceAccount": {"Ref": "AWS::AccountId"}},
                "ArnLike": {"aws:SourceArn": {"Fn::GetAtt": [source_bucket, "Arn"]}},
            }, template_name

            deny = statements["DenyInsecureTransport"]
            assert deny["Effect"] == "Deny" and deny["Principal"] == "*" and deny["Action"] == "s3:*"
            # Boolean false, not the string 'false' (guard rule compares a YAML boolean).
            assert deny["Condition"]["Bool"]["aws:SecureTransport"] is False
            assert deny["Resource"] == [
                {"Fn::GetAtt": [log_bucket, "Arn"]},
                {"Fn::Sub": f"${{{log_bucket}.Arn}}/*"},
            ]

    def test_every_log_delivery_grant_is_bound_to_its_actual_source_buckets(self):
        discovered_grants = []

        for path in sorted(INFRA_DIR.glob("*.yaml")):
            resources = load_infra_template(path.name).get("Resources", {})
            sources_by_destination = {}
            for source_name, resource in resources.items():
                if resource.get("Type") != "AWS::S3::Bucket":
                    continue
                logging = resource.get("Properties", {}).get("LoggingConfiguration")
                if not logging:
                    continue
                destination = logging["DestinationBucketName"]
                assert set(destination) == {"Ref"}, f"{path.name}/{source_name}: destination is not derivable"
                destination_name = destination["Ref"]
                sources_by_destination.setdefault(destination_name, []).append(
                    (source_name, logging.get("LogFilePrefix", ""))
                )

            grants_by_destination = {}
            for policy_name, resource in resources.items():
                if resource.get("Type") != "AWS::S3::BucketPolicy":
                    continue
                bucket = resource["Properties"]["Bucket"]
                statements = resource["Properties"]["PolicyDocument"]["Statement"]
                if isinstance(statements, dict):
                    statements = [statements]
                for statement in statements:
                    principal = statement.get("Principal", {})
                    if not isinstance(principal, dict):
                        continue
                    service = principal.get("Service")
                    services = service if isinstance(service, list) else [service]
                    if "logging.s3.amazonaws.com" not in services:
                        continue

                    assert bucket.keys() == {"Ref"}, f"{path.name}/{policy_name}: destination is not derivable"
                    destination_name = bucket["Ref"]
                    assert destination_name not in grants_by_destination, (
                        f"{path.name}/{destination_name}: multiple S3 log-delivery grants"
                    )
                    grants_by_destination[destination_name] = statement
                    discovered_grants.append(f"{path.name}/{policy_name}")

                    assert statement["Effect"] == "Allow"
                    actions = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
                    assert actions == ["s3:PutObject"]
                    condition = statement["Condition"]
                    assert condition["StringEquals"]["aws:SourceAccount"] == {"Ref": "AWS::AccountId"}

                    sources = sources_by_destination[destination_name]
                    expected_source_arns = [{"Fn::GetAtt": [source_name, "Arn"]} for source_name, _ in sources]
                    actual_source_arns = condition["ArnLike"]["aws:SourceArn"]
                    if len(expected_source_arns) > 1:
                        assert isinstance(actual_source_arns, list)
                    elif isinstance(actual_source_arns, dict):
                        actual_source_arns = [actual_source_arns]
                    assert {tuple(arn["Fn::GetAtt"]) for arn in actual_source_arns} == {
                        tuple(arn["Fn::GetAtt"]) for arn in expected_source_arns
                    }

                    policy_resources = (
                        statement["Resource"] if isinstance(statement["Resource"], list) else [statement["Resource"]]
                    )
                    marker = f"${{{destination_name}.Arn}}/"
                    delivery_paths = []
                    for policy_resource in policy_resources:
                        resource_sub = policy_resource.get("Fn::Sub") if isinstance(policy_resource, dict) else None
                        assert isinstance(resource_sub, str) and resource_sub.startswith(marker)
                        delivery_paths.append(resource_sub.removeprefix(marker))
                    for source_name, log_prefix in sources:
                        assert any(
                            path_pattern.endswith("*") and log_prefix.startswith(path_pattern.removesuffix("*"))
                            for path_pattern in delivery_paths
                        ), f"{path.name}/{source_name}: policy resource does not cover {log_prefix}"

            assert set(grants_by_destination) == set(sources_by_destination), path.name

        assert discovered_grants, "no logging.s3.amazonaws.com grants discovered"

    def test_log_destination_skips_cite_security_hub_s3_9(self):
        """The 8 CKV_AWS_18 skips stay (a log sink must not log to itself) and
        each comment carries the Security Hub S3.9 justification verbatim."""
        quote = (
            "The target logging bucket does not need to have server access logging enabled, "
            "and you should suppress findings for this bucket."
        )
        found = 0
        for path in sorted(INFRA_DIR.glob("*.yaml")):
            template = load_infra_template(path.name)
            for logical_id, resource in template.get("Resources", {}).items():
                for skip in resource.get("Metadata", {}).get("checkov", {}).get("skip", []):
                    if skip["id"] == "CKV_AWS_18":
                        found += 1
                        assert quote in skip["comment"], f"{path.name}/{logical_id}"
        assert found == 8
