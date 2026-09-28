# ABOUTME: Tests ALB OIDC claim verification in the landing-page distribution Lambda
# ABOUTME: Covers signer/alg/expiry validation, base64url decoding, and scoped ALB invoke permission

"""Tests for landing-page ALB identity handling.

The landing page trusts `x-amzn-oidc-data` to decide who may download packages.
ALB signs that header with ES256 and records its own ARN in the JWT header
`signer` field, so the backend must reject anything that is not a live token
from this deployment's load balancer
(https://docs.aws.amazon.com/elasticloadbalancing/latest/application/listener-authenticate-users.html).

The Lambda ships as inline `ZipFile` code, so these tests exec that code out of
the template and exercise `extract_user_email` directly.
"""

import base64
import builtins
import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

SOURCE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SOURCE_ROOT.parents[0]
LANDING_TEMPLATE = REPO_ROOT / "deployment" / "infrastructure" / "landing-page-distribution.yaml"

TEST_ALB_ARN = "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/gip-alb/abc123"


# --- CFN-aware YAML loader (handles !Ref, !Sub, !GetAtt, etc.) ---


class _CfnLoader(yaml.SafeLoader):
    pass


def _cfn_tag_constructor(loader, tag_suffix, node):
    """Resolve any !Tag to its scalar/sequence/mapping payload (value only)."""
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return None


_CfnLoader.add_multi_constructor("!", _cfn_tag_constructor)


def _template() -> dict:
    return yaml.load(LANDING_TEMPLATE.read_text(encoding="utf-8"), Loader=_CfnLoader)  # nosec B506


@pytest.fixture(scope="module")
def landing():
    """Exec the template's inline landing-page Lambda into a namespace."""
    code = _template()["Resources"]["LandingPageFunction"]["Properties"]["Code"]["ZipFile"]
    namespace: dict = {"__name__": "landing_page_inline"}
    env = {"S3_BUCKET_NAME": "test-bucket", "EXPECTED_ALB_ARN": TEST_ALB_ARN}
    with (
        patch.dict("os.environ", env, clear=False),
        patch("boto3.client", return_value=MagicMock()),
    ):
        builtins.exec(compile(code, "landing-page-distribution.yaml:ZipFile", "exec"), namespace)  # nosec B102 -- repo-owned template code
    return namespace


def _b64url(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


def _token(header_overrides=None, payload_overrides=None) -> str:
    header = {
        "alg": "ES256",
        "kid": "12345678-1234-1234-1234-123456789012",
        "signer": TEST_ALB_ARN,
        "exp": int(time.time()) + 300,
    }
    header.update(header_overrides or {})
    payload = {"sub": "user-sub", "email": "alice@company.com"}
    payload.update(payload_overrides or {})
    return f"{_b64url(header)}.{_b64url(payload)}.c2lnbmF0dXJlLWJ5dGVz"


def _event(token) -> dict:
    return {"headers": {"x-amzn-oidc-data": token}}


class TestAlbClaimVerification:
    def test_valid_token_returns_identity(self, landing):
        assert landing["extract_user_email"](_event(_token())) == "alice@company.com"

    def test_missing_header_is_rejected(self, landing):
        with pytest.raises(PermissionError):
            landing["extract_user_email"]({"headers": {}})

    def test_non_jwt_shape_is_rejected(self, landing):
        with pytest.raises(PermissionError):
            landing["extract_user_email"](_event("not.a-jwt"))

    def test_foreign_signer_is_rejected(self, landing):
        """Claims signed by a different load balancer must not be trusted."""
        other = "arn:aws:elasticloadbalancing:us-east-1:444455556666:loadbalancer/app/evil/xyz"
        with pytest.raises(PermissionError):
            landing["extract_user_email"](_event(_token({"signer": other})))

    def test_missing_signer_is_rejected(self, landing):
        with pytest.raises(PermissionError):
            landing["extract_user_email"](_event(_token({"signer": None})))

    @pytest.mark.parametrize("alg", ["none", "HS256", "RS256"])
    def test_unexpected_algorithm_is_rejected(self, landing, alg):
        """ALB only ever signs with ES256; anything else is a forged header."""
        with pytest.raises(PermissionError):
            landing["extract_user_email"](_event(_token({"alg": alg})))

    def test_expired_header_exp_is_rejected(self, landing):
        with pytest.raises(PermissionError):
            landing["extract_user_email"](_event(_token({"exp": int(time.time()) - 1})))

    def test_expired_payload_exp_is_rejected(self, landing):
        with pytest.raises(PermissionError):
            landing["extract_user_email"](_event(_token(payload_overrides={"exp": int(time.time()) - 60})))

    def test_missing_exp_is_rejected(self, landing):
        """Fail closed rather than accept claims with no expiry."""
        with pytest.raises(PermissionError):
            landing["extract_user_email"](_event(_token({"exp": None})))

    def test_non_numeric_exp_is_rejected(self, landing):
        with pytest.raises(PermissionError):
            landing["extract_user_email"](_event(_token({"exp": "not-a-number"})))

    def test_identity_claim_fallbacks(self, landing):
        token = _token(payload_overrides={"email": None, "preferred_username": "bob@company.com"})
        assert landing["extract_user_email"](_event(token)) == "bob@company.com"

        token = _token(payload_overrides={"email": None, "preferred_username": None, "upn": None})
        assert landing["extract_user_email"](_event(token)) == "user-sub"

    def test_no_identity_claim_is_rejected(self, landing):
        token = _token(payload_overrides={"email": None, "sub": None})
        with pytest.raises(PermissionError):
            landing["extract_user_email"](_event(token))

    def test_base64url_segments_decode(self, landing):
        """ALB base64url-encodes segments, so '-' and '_' must decode correctly.

        Standard base64 decoding silently drops characters outside its alphabet,
        which corrupts or rejects otherwise valid tokens.
        """
        payload = None
        for filler in range(300):
            candidate = {"sub": "user-sub", "email": "alice@company.com", "pad": "?>" * filler}
            if any(ch in _b64url(candidate) for ch in "-_"):
                payload = candidate
                break
        assert payload is not None, "could not construct a urlsafe-distinct payload"

        token = f"{_b64url({'alg': 'ES256', 'signer': TEST_ALB_ARN, 'exp': int(time.time()) + 300})}.{_b64url(payload)}.c2ln"
        assert landing["extract_user_email"](_event(token)) == "alice@company.com"


class TestTemplateControls:
    def test_alb_invoke_permission_is_scoped_to_this_target_group(self):
        """A wildcard target-group ARN lets any same-account target group invoke."""
        permission = _template()["Resources"]["LambdaInvokePermission"]["Properties"]
        source_arn = str(permission["SourceArn"])

        assert "targetgroup/*" not in source_arn
        assert "gip-lp-" in source_arn

    def test_target_group_has_deterministic_name(self):
        """The scoped SourceArn depends on a predictable target-group name."""
        target_group = _template()["Resources"]["LambdaTargetGroup"]["Properties"]
        assert "gip-lp-" in str(target_group["Name"])

    def test_function_receives_expected_alb_arn(self):
        env = _template()["Resources"]["LandingPageFunction"]["Properties"]["Environment"]["Variables"]
        assert "EXPECTED_ALB_ARN" in env
