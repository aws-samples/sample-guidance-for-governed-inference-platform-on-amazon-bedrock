# ABOUTME: Structural parity tests across the six OIDC bedrock-auth-* CloudFormation templates
# ABOUTME: Pins every template to the okta reference; provider differences are asserted via EXCEPTIONS

"""Anti-drift parity tests for the OIDC auth templates (AXIOMS A1).

The six bedrock-auth-{okta,azure,auth0,google,generic,cognito-pool}.yaml
templates are intentionally parallel. This test canonicalizes each template
(rename the provider OIDC logical ID, substitute provider parameters with
placeholders, blank descriptions) and deep-compares it to the okta reference.
Legitimate provider differences live in EXCEPTIONS and are asserted exactly —
never ignored. bedrock-auth-idc.yaml is out of scope (different auth model).
"""

import copy
import json
import re
from pathlib import Path

import pytest
import yaml

INFRA_DIR = Path(__file__).parent.parent.parent / "deployment" / "infrastructure"
DEPLOY_PY = Path(__file__).parent.parent / "governed_inference_platform" / "cli" / "commands" / "deploy.py"

REFERENCE = "okta"
PROVIDERS = ["okta", "azure", "auth0", "google", "generic", "cognito-pool"]

# Provider-specific OIDC provider logical IDs, canonicalized to "OIDCProvider".
OIDC_LOGICAL_IDS = {
    "okta": "OktaOIDCProvider",
    "azure": "AzureOIDCProvider",
    "auth0": "Auth0OIDCProvider",
    "google": "GoogleOIDCProvider",
    "generic": "OidcProvider",
    "cognito-pool": "CognitoUserPoolOIDCProvider",
}

# Provider-specific Parameters, stripped from Parameters and substituted with
# canonical placeholders wherever they are referenced.
PROVIDER_PARAMS = {
    "okta": {"OktaDomain": "PROVIDER_DOMAIN", "OktaClientId": "PROVIDER_CLIENT_ID"},
    "azure": {"AzureTenantId": "PROVIDER_DOMAIN", "AzureClientId": "PROVIDER_CLIENT_ID"},
    "auth0": {"Auth0Domain": "PROVIDER_DOMAIN", "Auth0ClientId": "PROVIDER_CLIENT_ID"},
    "google": {"GoogleDomain": "PROVIDER_DOMAIN", "GoogleClientId": "PROVIDER_CLIENT_ID"},
    "generic": {
        "OidcIssuerUrl": "PROVIDER_DOMAIN",
        "OidcClientId": "PROVIDER_CLIENT_ID",
    },
    "cognito-pool": {
        "CognitoUserPoolDomain": "PROVIDER_DOMAIN",
        "CognitoUserPoolClientId": "PROVIDER_CLIENT_ID",
        "CognitoUserPoolId": "PROVIDER_POOL_ID",
    },
}

# Shared Parameters that must match in Type/Default/AllowedValues (modulo EXCEPTIONS).
SHARED_PARAMS = [
    "FederationType",
    # Optional IAM OIDC provider thumbprint override; every template exposes it so
    # `gip deploy` can carry an existing provider's list through an update.
    "OidcThumbprintList",
    "IdentityPoolName",
    "FederatedRoleName",
    "AllowedBedrockRegions",
    "RestrictToAnthropicModels",
    "EnableMonitoring",
    "SessionNameBinding",
]

# deploy.py template_map keys per provider.
DEPLOY_KEYS = {
    "okta": "okta",
    "azure": "azure",
    "auth0": "auth0",
    "google": "google",
    "generic": "generic",
    "cognito-pool": "cognito",
}


class Absent:
    """Marker: the path must NOT exist for this provider."""

    def __repr__(self):
        return "<ABSENT>"


ABSENT = Absent()


def _config_json(provider_type, id_fields, mode):
    """Render the expected ConfigurationJson Sub body (post-placeholder substitution)."""
    lines = ["{", f'  "federation_type": "{mode}",', f'  "provider_type": "{provider_type}",']
    for key, placeholder in id_fields:
        lines.append(f'  "{key}": "${{{placeholder}}}",')
    if mode == "direct":
        lines.append('  "federated_role_arn": "${DirectIAMRole.Arn}",')
        lines.append('  "aws_region": "${AWS::Region}",')
        lines.append('  "max_session_duration": 43200')
    else:
        lines.append('  "identity_pool_id": "${CognitoIdentityPool}",')
        lines.append('  "federated_role_arn": "${CognitoAuthenticatedRole.Arn}",')
        lines.append('  "aws_region": "${AWS::Region}"')
    lines.append("}")
    return "\n".join(lines) + "\n"


_STANDARD_ID_FIELDS = [("provider_domain", "PROVIDER_DOMAIN"), ("client_id", "PROVIDER_CLIENT_ID")]
CONFIG_ID_FIELDS = {
    "okta": ("okta", _STANDARD_ID_FIELDS),
    "azure": ("azure", [("azure_tenant_id", "PROVIDER_DOMAIN"), ("client_id", "PROVIDER_CLIENT_ID")]),
    "auth0": ("auth0", _STANDARD_ID_FIELDS),
    "google": ("google", _STANDARD_ID_FIELDS),
    "generic": ("generic", _STANDARD_ID_FIELDS),
    "cognito-pool": (
        "cognito",
        [
            ("cognito_user_pool_id", "PROVIDER_POOL_ID"),
            ("cognito_domain", "PROVIDER_DOMAIN"),
            ("client_id", "PROVIDER_CLIENT_ID"),
        ],
    ),
}


def _tags(provider_label, purpose):
    return [{"Key": "Purpose", "Value": purpose}, {"Key": "Provider", "Value": provider_label}]


def _provider_map(fn):
    return {p: fn(p) for p in PROVIDERS}


PROVIDER_LABELS = {
    "okta": "Okta",
    "azure": "Azure",
    "auth0": "Auth0",
    "google": "Google",
    "generic": "Generic OIDC",
    "cognito-pool": "CognitoUserPool",
}

PROVIDER_AUTH_LABELS = {
    "okta": "Okta",
    "azure": "Azure AD",
    "auth0": "Auth0",
    "google": "Google",
    "generic": "OIDC",
    "cognito-pool": "Cognito User Pool",
}

# Every legitimate provider-specific difference, keyed by canonical JSON path.
# Each entry maps provider -> exact expected value (ABSENT = key must not exist).
# Values are asserted during canonicalization; a wrong value fails the test.
EXCEPTIONS = {
    "Resources/OIDCProvider/Condition": {
        "okta": ABSENT,
        "azure": ABSENT,
        "auth0": ABSENT,
        "google": ABSENT,
        "generic": ABSENT,
        # cognito mode wires the user pool into the identity pool directly, so
        # the IAM OIDC provider is only created for direct federation.
        "cognito-pool": "UseDirectIAM",
    },
    "Resources/OIDCProvider/Properties/Url": {
        "okta": "https://${PROVIDER_DOMAIN}",
        "azure": "https://login.microsoftonline.com/${PROVIDER_DOMAIN}/v2.0",
        "auth0": "https://${PROVIDER_DOMAIN}/",
        "google": "https://${PROVIDER_DOMAIN}",
        "generic": "PROVIDER_DOMAIN",
        "cognito-pool": [
            "https://cognito-idp.${PoolRegion}.amazonaws.com/${PROVIDER_POOL_ID}",
            {"PoolRegion": [0, ["_", "PROVIDER_POOL_ID"]]},
        ],
    },
    "Resources/OIDCProvider/Properties/Tags": _provider_map(
        lambda p: _tags(PROVIDER_LABELS[p], f"Claude Code {PROVIDER_AUTH_LABELS[p]} Authentication")
    ),
    # auth0 cannot offer 'sub' binding (see above); everyone else offers all
    # three modes. Type and Default ('none') stay in the shared skeleton.
    "Parameters/SessionNameBinding/AllowedValues": {
        "okta": ["none", "email", "sub"],
        "azure": ["none", "email", "sub"],
        "auth0": ["none", "email"],
        "google": ["none", "email", "sub"],
        "generic": ["none", "email", "sub"],
        "cognito-pool": ["none", "email", "sub"],
    },
    "Resources/DirectIAMRole/Properties/Tags/1/Value": _provider_map(lambda p: PROVIDER_LABELS[p]),
    "Resources/CognitoAuthenticatedRole/Properties/Tags/1/Value": _provider_map(lambda p: PROVIDER_LABELS[p]),
    "Resources/CognitoUnauthenticatedRole/Properties/Tags/1/Value": _provider_map(lambda p: PROVIDER_LABELS[p]),
    "Resources/IdentityPoolPrincipalTag/Properties/IdentityProviderName": {
        "okta": "PROVIDER_DOMAIN",
        "azure": "login.microsoftonline.com/${PROVIDER_DOMAIN}/v2.0",
        "auth0": "PROVIDER_DOMAIN",
        "google": "PROVIDER_DOMAIN",
        # !Select [1, !Split ['://', !Ref OidcIssuerUrl]] — strip the scheme.
        "generic": [1, ["://", "PROVIDER_DOMAIN"]],
        "cognito-pool": [
            "cognito-idp.${PoolRegion}.amazonaws.com/${PROVIDER_POOL_ID}",
            {"PoolRegion": [0, ["_", "PROVIDER_POOL_ID"]]},
        ],
    },
    # cognito-pool wires the user pool into the identity pool natively; the
    # other providers attach the IAM OIDC provider ARN instead.
    "Resources/CognitoIdentityPool/Properties/OpenIdConnectProviderARNs": {
        "okta": ["OIDCProvider.Arn"],
        "azure": ["OIDCProvider.Arn"],
        "auth0": ["OIDCProvider.Arn"],
        "google": ["OIDCProvider.Arn"],
        "generic": ["OIDCProvider.Arn"],
        "cognito-pool": ABSENT,
    },
    "Resources/CognitoIdentityPool/Properties/CognitoIdentityProviders": {
        "okta": ABSENT,
        "azure": ABSENT,
        "auth0": ABSENT,
        "google": ABSENT,
        "generic": ABSENT,
        "cognito-pool": [
            {
                "ClientId": "PROVIDER_CLIENT_ID",
                "ProviderName": [
                    "cognito-idp.${PoolRegion}.amazonaws.com/${PROVIDER_POOL_ID}",
                    {"PoolRegion": [0, ["_", "PROVIDER_POOL_ID"]]},
                ],
                "ServerSideTokenCheck": True,
            }
        ],
    },
    # Cognito user pool ID tokens carry no top-level 'name' claim by default.
    "Resources/IdentityPoolPrincipalTag/Properties/PrincipalTags/UserName": {
        "okta": "name",
        "azure": "name",
        "auth0": "name",
        "google": "name",
        "generic": "name",
        "cognito-pool": ABSENT,
    },
    "Outputs/ConfigurationJson/Value/1": {
        p: _config_json(CONFIG_ID_FIELDS[p][0], CONFIG_ID_FIELDS[p][1], "direct") for p in PROVIDERS
    },
    "Outputs/ConfigurationJson/Value/2": {
        p: _config_json(CONFIG_ID_FIELDS[p][0], CONFIG_ID_FIELDS[p][1], "cognito") for p in PROVIDERS
    },
    "Parameters/IdentityPoolName/Default": {
        "okta": "gip-okta",
        "azure": "gip-azure",
        "auth0": "gip-auth0",
        "google": "gip-google",
        "generic": "gip-oidc",
        "cognito-pool": "gip-cognito",
    },
    "Parameters/FederatedRoleName/Default": {
        "okta": "BedrockOktaFederatedRole",
        "azure": "BedrockAzureFederatedRole",
        "auth0": "BedrockAuth0FederatedRole",
        "google": "BedrockGoogleFederatedRole",
        "generic": "BedrockOidcFederatedRole",
        "cognito-pool": "BedrockCognitoFederatedRole",
    },
}

ISSUER_KEYS = {
    "okta": "PROVIDER_DOMAIN",
    "azure": "login.microsoftonline.com/${PROVIDER_DOMAIN}/v2.0",
    "auth0": "PROVIDER_DOMAIN",
    "generic": [1, ["://", "PROVIDER_DOMAIN"]],
    "cognito-pool": [
        "",
        [
            "cognito-idp.",
            [0, ["_", "PROVIDER_POOL_ID"]],
            ".amazonaws.com/",
            "PROVIDER_POOL_ID",
        ],
    ],
}

SESSION_CLAIMS = {
    "okta": [
        "BindSessionToEmail",
        ["", ["${", "PROVIDER_DOMAIN", ":email}"]],
        ["", ["${", "PROVIDER_DOMAIN", ":sub}"]],
    ],
    "azure": [
        "BindSessionToEmail",
        ["", ["${login.microsoftonline.com/", "PROVIDER_DOMAIN", "/v2.0:email}"]],
        ["", ["${login.microsoftonline.com/", "PROVIDER_DOMAIN", "/v2.0:sub}"]],
    ],
    "auth0": ["", ["${", "PROVIDER_DOMAIN", ":email}"]],
    "generic": [
        "BindSessionToEmail",
        ["", ["${", [1, ["://", "PROVIDER_DOMAIN"]], ":email}"]],
        ["", ["${", [1, ["://", "PROVIDER_DOMAIN"]], ":sub}"]],
    ],
    "cognito-pool": [
        "BindSessionToEmail",
        [
            "",
            [
                "${cognito-idp.",
                [0, ["_", "PROVIDER_POOL_ID"]],
                ".amazonaws.com/",
                "PROVIDER_POOL_ID",
                ":email}",
            ],
        ],
        [
            "",
            [
                "${cognito-idp.",
                [0, ["_", "PROVIDER_POOL_ID"]],
                ".amazonaws.com/",
                "PROVIDER_POOL_ID",
                ":sub}",
            ],
        ],
    ],
}


class CFLoader(yaml.SafeLoader):
    """YAML loader that handles CloudFormation intrinsic functions."""


for tag in [
    "!Ref",
    "!Sub",
    "!GetAtt",
    "!If",
    "!Equals",
    "!Not",
    "!Select",
    "!Join",
    "!Split",
    "!FindInMap",
    "!Condition",
    "!Or",
    "!And",
]:
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


def _load_template(provider):
    path = INFRA_DIR / f"bedrock-auth-{provider}.yaml"
    with open(path, encoding="utf-8") as f:
        return yaml.load(f, Loader=CFLoader)  # nosec B506


def _blank_descriptions(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "Description" and isinstance(value, str):
                node[key] = ""
            else:
                _blank_descriptions(value)
    elif isinstance(node, list):
        for item in node:
            _blank_descriptions(item)


def _rewrite_scalars(node, mapping):
    """Replace scalar strings per mapping: exact matches, 'Old.Attr' refs, and ${Old} in Subs."""

    def rewrite(value):
        if not isinstance(value, str):
            return value
        for old, new in mapping.items():
            if value == old:
                return new
            if value.startswith(f"{old}."):
                return new + value[len(old) :]
            value = value.replace(f"${{{old}}}", f"${{{new}}}").replace(f"${{{old}.", f"${{{new}.")
        return value

    if isinstance(node, dict):
        return {rewrite(k): _rewrite_scalars(v, mapping) for k, v in node.items()}
    if isinstance(node, list):
        return [_rewrite_scalars(item, mapping) for item in node]
    return rewrite(node)


def _navigate(doc, segments):
    parent = None
    node = doc
    for seg in segments:
        key = int(seg) if seg.isdigit() else seg
        parent = node
        if isinstance(node, dict):
            if key not in node:
                return parent, key, ABSENT
            node = node[key]
        else:
            node = node[key]
    return parent, key, node


def _assert_direct_trust_policy(doc, provider):
    policy = doc["Resources"]["DirectIAMRole"]["Properties"]["AssumeRolePolicyDocument"]
    if provider == "google":
        condition = policy["Statement"][0]["Condition"]["StringEquals"]
        assert condition == {
            "accounts.google.com:aud": "PROVIDER_CLIENT_ID",
            "sts:RoleSessionName": [
                "BindSessionName",
                ["BindSessionToEmail", "${accounts.google.com:email}", "${accounts.google.com:sub}"],
                "AWS::NoValue",
            ],
        }
        return

    assert policy[0] == "BindSessionName"
    for branch, has_session_binding in zip(policy[1:], [True, False], strict=True):
        rendered, variables = branch
        statement = json.loads(rendered)["Statement"][0]
        expected_conditions = {"${Issuer}:aud": "${ClientId}"}
        if has_session_binding:
            expected_conditions["sts:RoleSessionName"] = "${SessionClaim}"
        assert statement == {
            "Effect": "Allow",
            "Principal": {"Federated": "${ProviderArn}"},
            "Action": ["sts:AssumeRoleWithWebIdentity", "sts:TagSession"],
            "Condition": {"StringEquals": expected_conditions},
        }
        assert variables["ProviderArn"] == "OIDCProvider.Arn"
        assert variables["Issuer"] == ISSUER_KEYS[provider]
        assert variables["ClientId"] == "PROVIDER_CLIENT_ID"
        if has_session_binding:
            assert variables["SessionClaim"] == SESSION_CLAIMS[provider]
        else:
            assert "SessionClaim" not in variables


def canonicalize(template, provider):
    doc = copy.deepcopy(template)
    _blank_descriptions(doc)

    mapping = dict(PROVIDER_PARAMS[provider])
    old_id = OIDC_LOGICAL_IDS[provider]
    if old_id != "OIDCProvider":
        mapping[old_id] = "OIDCProvider"
    doc = _rewrite_scalars(doc, mapping)

    _assert_direct_trust_policy(doc, provider)
    doc["Resources"]["DirectIAMRole"]["Properties"]["AssumeRolePolicyDocument"] = "<DIRECT_TRUST_POLICY>"

    for param in PROVIDER_PARAMS[provider]:
        assert param in template.get("Parameters", {}), f"{provider}: expected provider parameter {param}"
        doc["Parameters"].pop(mapping[param], None)
        doc["Parameters"].pop(param, None)

    for path, expected_by_provider in EXCEPTIONS.items():
        assert provider in expected_by_provider, f"EXCEPTIONS[{path}] missing entry for {provider}"
        expected = expected_by_provider[provider]
        parent, key, actual = _navigate(doc, path.split("/"))
        if expected is ABSENT:
            assert actual is ABSENT, f"{provider}: expected nothing at {path}, found {actual!r}"
        else:
            assert actual is not ABSENT, f"{provider}: missing expected exception value at {path}"
            assert actual == expected, f"{provider}: exception mismatch at {path}: {actual!r} != {expected!r}"
        parent[key] = f"<EXCEPTION:{path}>"
    return doc


def _diff(ref, other, path, out):
    if isinstance(ref, dict) and isinstance(other, dict):
        for key in sorted(set(ref) | set(other), key=str):
            child = f"{path}/{key}"
            if key not in ref:
                out.append(f"{child}: only in candidate ({other[key]!r})")
            elif key not in other:
                out.append(f"{child}: only in {REFERENCE} ({ref[key]!r})")
            else:
                _diff(ref[key], other[key], child, out)
    elif isinstance(ref, list) and isinstance(other, list):
        if len(ref) != len(other):
            out.append(f"{path}: list length {len(ref)} ({REFERENCE}) != {len(other)} (candidate)")
        for i, (r, o) in enumerate(zip(ref, other, strict=False)):
            _diff(r, o, f"{path}/{i}", out)
    elif ref != other:
        out.append(f"{path}: {ref!r} ({REFERENCE}) != {other!r} (candidate)")


@pytest.fixture(scope="module")
def canonical():
    return {p: canonicalize(_load_template(p), p) for p in PROVIDERS}


NON_REFERENCE = [p for p in PROVIDERS if p != REFERENCE]


class TestTemplateParity:
    """Every OIDC auth template must be structurally identical to okta after canonicalization."""

    @pytest.mark.parametrize("provider", NON_REFERENCE)
    def test_resource_skeleton_matches_reference(self, canonical, provider):
        diffs = []
        _diff(canonical[REFERENCE]["Resources"], canonical[provider]["Resources"], "Resources", diffs)
        assert not diffs, f"{provider} drifted from {REFERENCE}:\n" + "\n".join(diffs)

    @pytest.mark.parametrize("provider", NON_REFERENCE)
    def test_conditions_match_reference(self, canonical, provider):
        diffs = []
        _diff(canonical[REFERENCE]["Conditions"], canonical[provider]["Conditions"], "Conditions", diffs)
        assert not diffs, f"{provider} Conditions drifted:\n" + "\n".join(diffs)

    @pytest.mark.parametrize("provider", NON_REFERENCE)
    @pytest.mark.parametrize("param", SHARED_PARAMS)
    def test_shared_parameters_match_reference(self, canonical, provider, param):
        ref_params = canonical[REFERENCE]["Parameters"]
        params = canonical[provider]["Parameters"]
        assert param in params, f"{provider} missing shared parameter {param}"
        for field in ["Type", "Default", "AllowedValues"]:
            assert params[param].get(field) == ref_params[param].get(field), (
                f"{provider} Parameters/{param}/{field}: "
                f"{params[param].get(field)!r} != {ref_params[param].get(field)!r} ({REFERENCE})"
            )

    @pytest.mark.parametrize("provider", NON_REFERENCE)
    def test_output_names_match_reference(self, canonical, provider):
        ref_names = set(canonical[REFERENCE]["Outputs"])
        names = set(canonical[provider]["Outputs"])
        assert names == ref_names, (
            f"{provider} Outputs drifted: missing={sorted(ref_names - names)} extra={sorted(names - ref_names)}"
        )


class TestReferenceAnchors:
    """Pin the security-relevant bits of the okta reference so parity can't drift in lockstep."""

    @pytest.mark.parametrize("provider", PROVIDERS)
    def test_direct_trust_policy_pins_client_audience_and_preserves_binding(self, provider):
        canonicalize(_load_template(provider), provider)

    def test_invoke_statements_grant_bearer_and_discovery_actions(self, canonical):
        statements = canonical[REFERENCE]["Resources"]["BedrockAccessPolicy"]["Properties"]["PolicyDocument"][
            "Statement"
        ]
        expected = [
            "bedrock:InvokeModel",
            "bedrock:InvokeModelWithResponseStream",
            "bedrock:CallWithBearerToken",
            "bedrock:ListFoundationModels",
            "bedrock:ListInferenceProfiles",
        ]
        by_sid = {s.get("Sid"): s for s in statements if isinstance(s, dict)}
        for sid in ["AllowBedrockInvokeRegional", "AllowBedrockInvokeGlobal"]:
            assert by_sid[sid]["Action"] == expected, f"{sid} actions drifted: {by_sid[sid]['Action']}"
        regional = by_sid["AllowBedrockInvokeRegional"]["Resource"]
        assert not any("application-inference-profile/*" in arn for arn in regional[1]), (
            "Anthropic-only branch must not allow opaque application-inference-profile ARNs"
        )
        assert any("application-inference-profile/*" in arn for arn in regional[2]), (
            "Unrestricted branch missing application-inference-profile ARN"
        )

    def test_oidc_provider_thumbprint_list_is_optional_and_parameterized(self, canonical):
        """ThumbprintList must be !If [HasOidcThumbprints, !Ref OidcThumbprintList, AWS::NoValue]
        in every template: no literal thumbprints (IAM retrieves the CA thumbprint itself on
        create) and a parameter to carry an existing provider's list through an update, because
        IAM rejects removing it (UpdateOpenIDConnectProviderThumbprint with null -> 400)."""
        for provider in PROVIDERS:
            doc = canonical[provider]
            assert doc["Parameters"]["OidcThumbprintList"]["Type"] == "CommaDelimitedList", provider
            assert doc["Parameters"]["OidcThumbprintList"]["Default"] == "", provider
            assert doc["Conditions"]["HasOidcThumbprints"] == [[["", "OidcThumbprintList"], ""]], provider
            props = doc["Resources"]["OIDCProvider"]["Properties"]
            assert props["ThumbprintList"] == ["HasOidcThumbprints", "OidcThumbprintList", "AWS::NoValue"], provider

    def test_mantle_endpoint_is_explicitly_denied(self, canonical):
        statements = canonical[REFERENCE]["Resources"]["BedrockAccessPolicy"]["Properties"]["PolicyDocument"][
            "Statement"
        ]
        by_sid = {s.get("Sid"): s for s in statements if isinstance(s, dict)}
        deny = by_sid.get("DenyBedrockMantleEndpoint")
        assert deny == {
            "Sid": "DenyBedrockMantleEndpoint",
            "Effect": "Deny",
            "Action": "bedrock-mantle:*",
            "Resource": "*",
        }

    def test_cloudwatch_metrics_grant_is_namespace_scoped(self, canonical):
        for provider in PROVIDERS:
            statements = canonical[provider]["Resources"]["BedrockAccessPolicy"]["Properties"]["PolicyDocument"][
                "Statement"
            ]
            metrics = [
                s
                for s in statements
                if isinstance(s, list) and isinstance(s[1], dict) and s[1].get("Sid") == "AllowCloudWatchMetrics"
            ]
            assert len(metrics) == 1, f"{provider}: expected one conditional AllowCloudWatchMetrics statement"
            condition = metrics[0][1].get("Condition")
            assert condition == {"StringEquals": {"cloudwatch:namespace": ["GIP/Bedrock/Usage", "AWS/Bedrock"]}}, (
                f"{provider}: cloudwatch:PutMetricData must be scoped to the usage namespaces, got {condition!r}"
            )


class TestExceptionsAreLive:
    """Every EXCEPTIONS entry must earn its keep: at least two providers must actually differ."""

    @pytest.mark.parametrize("path", sorted(EXCEPTIONS))
    def test_exception_differs_somewhere(self, path):
        entries = EXCEPTIONS[path]
        assert set(entries) == set(PROVIDERS), f"EXCEPTIONS[{path}] must cover all providers"
        reference_value = entries[REFERENCE]
        marker = repr(reference_value) if reference_value is not ABSENT else "<ABSENT>"
        assert any(repr(entries[p]) != marker for p in NON_REFERENCE), (
            f"EXCEPTIONS[{path}] is dead weight: every provider matches {REFERENCE}; fold it into the skeleton"
        )


class TestDeployMapParity:
    """deploy.py's provider->template maps must cover exactly the templates on disk."""

    def test_deploy_map_covers_all_templates(self):
        src = DEPLOY_PY.read_text(encoding="utf-8")
        pairs = re.findall(r'"([a-z0-9-]+)":\s*"(bedrock-auth-[a-z0-9-]+\.yaml)"', src)
        mapping = {}
        for key, template in pairs:
            mapping.setdefault(key, set()).add(template)
        for provider in PROVIDERS:
            key = DEPLOY_KEYS[provider]
            expected = f"bedrock-auth-{provider}.yaml"
            assert mapping.get(key) == {expected}, (
                f"deploy.py maps {key!r} to {mapping.get(key)}, expected {{{expected!r}}}"
            )
            assert pairs.count((key, expected)) == 2, (
                f"deploy.py should map {key!r} in both template_map copies, found {pairs.count((key, expected))}"
            )
        on_disk = {p.name for p in INFRA_DIR.glob("bedrock-auth-*.yaml")} - {"bedrock-auth-idc.yaml"}
        assert on_disk == {f"bedrock-auth-{p}.yaml" for p in PROVIDERS}, (
            f"templates on disk out of sync with PROVIDERS: {sorted(on_disk)}"
        )
