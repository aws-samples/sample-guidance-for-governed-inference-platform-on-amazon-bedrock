# ABOUTME: Contract tests for deployment/infrastructure/s3bucket.yaml
# ABOUTME: Pins the opt-in OrganizationId org-read policy and the single-account default posture

"""Template contract tests for the CFN-artifacts bucket stack.

These pin the multi-account limitation-L3 fix: the optional org-read bucket
policy statement must stay opt-in (empty ``OrganizationId`` = no org-read
grant, single-account posture) and, when enabled, must grant only
s3:GetObject gated on aws:PrincipalOrgID. Same parameter pattern as
skills-registry.yaml. The BucketPolicy resource itself is unconditional
since the ws4 scan hardening: it always carries the TLS-only deny statement.
"""

from pathlib import Path

import yaml

TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "deployment" / "infrastructure" / "s3bucket.yaml"


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


def test_organization_id_defaults_to_single_account():
    """Empty default keeps existing single-account deployments bit-identical."""
    params = _template()["Parameters"]
    assert params["OrganizationId"]["Default"] == ""
    assert params["OrganizationId"]["AllowedPattern"] == "^$|^o-[a-z0-9]{10,32}$"


def test_bucket_policy_is_conditional_on_org():
    """The org-read grant only exists when OrganizationId is set.

    Since the ws4 scan hardening the BucketPolicy resource itself is
    always present (it carries the unconditional TLS-only deny statement,
    S3_BUCKET_SSL_REQUESTS_ONLY); a bucket allows only one BucketPolicy, so
    the org-read statement is gated inside it with Fn::If on IsOrg instead
    of a resource-level Condition. Parameter contract is unchanged.
    """
    template = _template()
    assert "IsOrg" in template["Conditions"]
    policy = template["Resources"]["CfnArtifactsBucketPolicy"]
    assert policy["Type"] == "AWS::S3::BucketPolicy"
    assert "Condition" not in policy, "resource must stay unconditional so the TLS deny always applies"
    statements = policy["Properties"]["PolicyDocument"]["Statement"]
    org_ifs = [s for s in statements if isinstance(s, list)]
    assert len(org_ifs) == 1, "org-read statement must be gated with Fn::If"
    condition_name, _, else_branch = org_ifs[0]
    assert condition_name == "IsOrg"
    assert else_branch == "AWS::NoValue"


def test_org_read_grants_only_get_object_gated_on_org_id():
    """Least privilege: s3:GetObject on objects only, gated on aws:PrincipalOrgID."""
    policy = _template()["Resources"]["CfnArtifactsBucketPolicy"]
    statements = policy["Properties"]["PolicyDocument"]["Statement"]
    org_statement = next(s[1] for s in statements if isinstance(s, list))
    assert org_statement["Effect"] == "Allow"
    assert org_statement["Action"] == "s3:GetObject"
    assert "aws:PrincipalOrgID" in org_statement["Condition"]["StringEquals"]


def test_tls_only_deny_is_unconditional():
    """ws4 hardening: every request must use TLS regardless of OrganizationId."""
    policy = _template()["Resources"]["CfnArtifactsBucketPolicy"]
    statements = policy["Properties"]["PolicyDocument"]["Statement"]
    deny = next(s for s in statements if isinstance(s, dict) and s.get("Effect") == "Deny")
    assert deny["Principal"] == "*"
    assert deny["Action"] == "s3:*"
    # Boolean false (not string 'false'): the S3_BUCKET_SSL_REQUESTS_ONLY
    # guard rule compares against a YAML boolean; IAM accepts either form.
    assert deny["Condition"]["Bool"]["aws:SecureTransport"] is False
