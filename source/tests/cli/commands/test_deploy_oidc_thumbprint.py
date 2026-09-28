# ABOUTME: Regression tests — `gip deploy auth` and the optional IAM OIDC provider ThumbprintList
# ABOUTME: New stacks pass nothing; existing providers' thumbprints are preserved; profile values still win

"""How `gip deploy auth` maps thumbprints onto the bedrock-auth-*.yaml `OidcThumbprintList` parameter.

Live-verified IAM behaviour that drives this:
- CREATE of AWS::IAM::OIDCProvider without ThumbprintList succeeds (IAM fills the CA thumbprint);
- UPDATE of a provider that HAS a ThumbprintList to a template that omits it FAILS:
  UpdateOpenIDConnectProviderThumbprint is called with null -> 400 "Member must not be null".

So `gip deploy` must:
- pass nothing for a new stack (IAM retrieves the CA thumbprint itself);
- on update, re-send the thumbprints IAM currently holds (read via GetOpenIDConnectProvider,
  never computed) when the profile has none;
- keep passing a generic profile's own oidc_thumbprint (older profiles, private-CA hosts);
- stop with a clear error when the current list cannot be read, instead of running into the
  known-failing update.
"""

import dataclasses
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from governed_inference_platform.cli.commands.deploy import (
    DeployCommand,
    OidcThumbprintLookupError,
    _existing_oidc_provider_thumbprints,
)
from governed_inference_platform.cli.utils.cloudformation import CloudFormationManager
from governed_inference_platform.config import Profile

THUMBPRINT = "9e99a48a9960b14926bb7f3b02e22da2b0ab7280"
IAM_FILLED = "08745487e891c19e3078c1f2a07e452950ef36f6"  # what IAM populated for accounts.google.com in the sandbox
PROVIDER_ARN = "arn:aws:iam::123456789012:oidc-provider/auth.example.com"

PROVIDER_PROFILES = {
    "okta": {"provider_domain": "company.okta.com", "client_id": "0oa0000000000000000"},
    "auth0": {"provider_domain": "company.auth0.com", "client_id": "auth0-client-id-0000"},
    "azure": {
        "provider_domain": "login.microsoftonline.com/12345678-1234-1234-1234-123456789012/v2.0",
        "client_id": "12345678-1234-1234-1234-123456789012",
    },
    "google": {"provider_domain": "accounts.google.com", "client_id": "1234-abcd.apps.googleusercontent.com"},
    "cognito": {
        "provider_domain": "my-pool.auth.us-east-1.amazoncognito.com",
        "client_id": "cognitoclientid000000",
        "cognito_user_pool_id": "us-east-1_AbCdEfGhI",
    },
    "generic": {
        "provider_domain": "auth.example.com",
        "client_id": "bedrock-cli-prod",
        "oidc_issuer_url": "https://auth.example.com",
        "oidc_authorization_endpoint": "https://auth.example.com/as/authorization.oauth2",
        "oidc_token_endpoint": "https://auth.example.com/as/token.oauth2",
        "oidc_jwks_uri": "https://auth.example.com/pf/JWKS",
    },
}


def _make_profile(provider_type="generic", **overrides):
    field_names = {f.name for f in dataclasses.fields(Profile)}
    defaults = {
        "name": f"{provider_type}-test",
        "credential_storage": "session",
        "aws_region": "us-east-1",
        "identity_pool_name": "gip-test",
        "sso_enabled": True,
        "provider_type": provider_type,
        "monitoring_enabled": False,
        "quota_monitoring_enabled": False,
        "federation_type": "direct",
        "federated_role_arn": "arn:aws:iam::123456789012:role/BedrockRole",
    }
    defaults.update(PROVIDER_PROFILES[provider_type])
    defaults.update(overrides)
    return Profile(**{k: v for k, v in defaults.items() if k in field_names})


def _client_error(code, operation):
    return ClientError({"Error": {"Code": code, "Message": f"{code} for test"}}, operation)


def _deploy_auth(profile, *, existing_provider_arns=(), iam_thumbprints=(), iam_error=None, cfn_error=None):
    """Run the auth-stack deploy against mocked CloudFormation + IAM; return (rc, params, console, iam)."""
    mock_manager = MagicMock(spec=CloudFormationManager)
    mock_manager.region = profile.aws_region
    mock_result = MagicMock()
    mock_result.success = True
    mock_result.outputs = {}
    mock_manager.deploy_stack.return_value = mock_result
    if cfn_error is not None:
        mock_manager.get_stack_resource_physical_ids.side_effect = cfn_error
    else:
        mock_manager.get_stack_resource_physical_ids.return_value = list(existing_provider_arns)

    iam = MagicMock()
    if iam_error is not None:
        iam.get_open_id_connect_provider.side_effect = iam_error
    else:
        iam.get_open_id_connect_provider.return_value = {"ThumbprintList": list(iam_thumbprints)}

    command = DeployCommand()
    console = MagicMock()
    console.print = MagicMock()

    with (
        patch("governed_inference_platform.cli.commands.deploy.CloudFormationManager", return_value=mock_manager),
        patch("boto3.client", return_value=iam) as boto_client,
    ):
        rc = command._deploy_stack("auth", profile, console, mock_manager)

    if boto_client.called:
        assert boto_client.call_args.args == ("iam",)
        assert boto_client.call_args.kwargs == {"region_name": profile.aws_region}

    call = mock_manager.deploy_stack.call_args
    params = None
    if call is not None:
        params = {p["ParameterKey"]: p["ParameterValue"] for p in call.kwargs["parameters"]}
    return rc, params, console, iam


def _printed(console):
    return " ".join(str(c) for c in console.print.call_args_list)


ALL_PROVIDERS = sorted(PROVIDER_PROFILES)


class TestNewStackPassesNoThumbprint:
    @pytest.mark.parametrize("provider_type", ALL_PROVIDERS)
    def test_new_stack_omits_parameter_so_iam_retrieves_the_ca_thumbprint(self, provider_type):
        rc, params, console, iam = _deploy_auth(_make_profile(provider_type))

        assert rc == 0, _printed(console)
        assert params is not None, "deploy_stack was never called"
        assert "OidcThumbprintList" not in params
        iam.get_open_id_connect_provider.assert_not_called()


class TestExistingStackPreservesIamThumbprints:
    @pytest.mark.parametrize("provider_type", ALL_PROVIDERS)
    def test_existing_provider_thumbprints_are_carried_through(self, provider_type):
        """Regression for the live-verified update failure: the current list is re-sent verbatim."""
        rc, params, console, iam = _deploy_auth(
            _make_profile(provider_type),
            existing_provider_arns=[PROVIDER_ARN],
            iam_thumbprints=[IAM_FILLED, THUMBPRINT],
        )

        assert rc == 0, _printed(console)
        assert params["OidcThumbprintList"] == f"{IAM_FILLED},{THUMBPRINT}"
        iam.get_open_id_connect_provider.assert_called_once_with(OpenIDConnectProviderArn=PROVIDER_ARN)
        assert "Preserving 2 existing" in _printed(console)

    def test_existing_stack_without_oidc_provider_passes_nothing(self):
        """cognito-pool in cognito federation mode has no IAM OIDC provider resource."""
        rc, params, _, iam = _deploy_auth(
            _make_profile("cognito", federation_type="cognito"), existing_provider_arns=[]
        )

        assert rc == 0
        assert "OidcThumbprintList" not in params
        iam.get_open_id_connect_provider.assert_not_called()

    def test_existing_provider_with_empty_list_passes_nothing(self):
        rc, params, _, _ = _deploy_auth(
            _make_profile("okta"), existing_provider_arns=[PROVIDER_ARN], iam_thumbprints=[]
        )

        assert rc == 0
        assert "OidcThumbprintList" not in params


class TestProfileThumbprintStillWins:
    def test_generic_profile_value_is_passed_and_iam_is_not_consulted(self):
        """Backward compat: an older generic profile carrying oidc_thumbprint deploys it unchanged."""
        rc, params, _, iam = _deploy_auth(
            _make_profile("generic", oidc_thumbprint=THUMBPRINT),
            existing_provider_arns=[PROVIDER_ARN],
            iam_thumbprints=[IAM_FILLED],
        )

        assert rc == 0
        assert params["OidcThumbprintList"] == THUMBPRINT
        iam.get_open_id_connect_provider.assert_not_called()

    def test_generic_comma_separated_profile_value_passed_verbatim(self):
        two = f"{THUMBPRINT},60b5e7d8fbad8a16a1caf68d01354c20bb1f8620"
        rc, params, _, _ = _deploy_auth(_make_profile("generic", oidc_thumbprint=two))

        assert rc == 0
        assert params["OidcThumbprintList"] == two

    def test_missing_generic_issuer_is_still_a_hard_error(self):
        rc, params, console, _ = _deploy_auth(_make_profile("generic", oidc_issuer_url=None))

        assert rc == 1
        assert params is None
        assert "oidc_issuer_url" in _printed(console)
        assert "oidc_thumbprint" not in _printed(console)


class TestLookupFailuresStopTheDeploy:
    def test_iam_read_failure_is_a_clear_error_and_no_update_is_attempted(self):
        rc, params, console, _ = _deploy_auth(
            _make_profile("okta"),
            existing_provider_arns=[PROVIDER_ARN],
            iam_error=_client_error("AccessDenied", "GetOpenIDConnectProvider"),
        )

        assert rc == 1
        assert params is None, "deploy_stack must not be called when the current list is unknown"
        printed = _printed(console)
        assert "iam:GetOpenIDConnectProvider" in printed
        assert PROVIDER_ARN in printed
        assert "AccessDenied" in printed

    def test_cloudformation_read_failure_is_a_clear_error(self):
        rc, params, console, _ = _deploy_auth(
            _make_profile("okta"), cfn_error=_client_error("AccessDenied", "DescribeStackResources")
        )

        assert rc == 1
        assert params is None
        assert "cloudformation:DescribeStackResources" in _printed(console)


class TestRetryAfterFailedFirstDeploy:
    """A ROLLBACK_COMPLETE auth stack still lists its AWS::IAM::OIDCProvider row (status
    DELETE_COMPLETE, old ARN retained) and the lookup runs BEFORE deploy_stack() deletes and
    re-creates the stack. The provider is gone, so there is nothing to preserve: the retry
    must reach the create path instead of dying on a misleading permission error."""

    def test_deleted_provider_no_such_entity_proceeds_with_the_create_path(self):
        rc, params, console, iam = _deploy_auth(
            _make_profile("okta"),
            existing_provider_arns=[PROVIDER_ARN],  # stale ARN from the rolled-back stack
            iam_error=_client_error("NoSuchEntity", "GetOpenIDConnectProvider"),
        )

        assert rc == 0, _printed(console)
        assert params is not None, "deploy_stack must be reached so ROLLBACK_COMPLETE cleanup can run"
        assert "OidcThumbprintList" not in params
        iam.get_open_id_connect_provider.assert_called_once_with(OpenIDConnectProviderArn=PROVIDER_ARN)
        assert "grant the permission" not in _printed(console)

    def test_access_denied_still_stops_even_when_another_provider_is_gone(self):
        manager = MagicMock(spec=CloudFormationManager)
        manager.region = "us-east-1"
        manager.get_stack_resource_physical_ids.return_value = [PROVIDER_ARN + "-gone", PROVIDER_ARN]
        iam = MagicMock()
        iam.get_open_id_connect_provider.side_effect = [
            _client_error("NoSuchEntity", "GetOpenIDConnectProvider"),
            _client_error("AccessDenied", "GetOpenIDConnectProvider"),
        ]

        with patch("boto3.client", return_value=iam), pytest.raises(OidcThumbprintLookupError) as exc:
            _existing_oidc_provider_thumbprints(manager, "gip-stack")

        assert "iam:GetOpenIDConnectProvider" in str(exc.value)
        assert PROVIDER_ARN in str(exc.value)
        assert "AccessDenied" in str(exc.value)


class TestExistingOidcProviderThumbprintsHelper:
    def test_returns_empty_for_new_stack(self):
        manager = MagicMock(spec=CloudFormationManager)
        manager.region = "us-east-1"
        manager.get_stack_resource_physical_ids.return_value = []

        with patch("boto3.client") as boto_client:
            assert _existing_oidc_provider_thumbprints(manager, "gip-stack") == []
        boto_client.assert_not_called()

    def test_no_such_entity_means_nothing_to_preserve(self):
        manager = MagicMock(spec=CloudFormationManager)
        manager.region = "us-east-1"
        manager.get_stack_resource_physical_ids.return_value = [PROVIDER_ARN + "-gone", PROVIDER_ARN]
        iam = MagicMock()
        iam.get_open_id_connect_provider.side_effect = [
            _client_error("NoSuchEntity", "GetOpenIDConnectProvider"),
            {"ThumbprintList": [THUMBPRINT]},
        ]

        with patch("boto3.client", return_value=iam):
            assert _existing_oidc_provider_thumbprints(manager, "gip-stack") == [THUMBPRINT]

    def test_deduplicates_and_drops_blank_entries_across_providers(self):
        manager = MagicMock(spec=CloudFormationManager)
        manager.region = "us-east-1"
        manager.get_stack_resource_physical_ids.return_value = [PROVIDER_ARN, PROVIDER_ARN + "-2"]
        iam = MagicMock()
        iam.get_open_id_connect_provider.side_effect = [
            {"ThumbprintList": [THUMBPRINT, ""]},
            {"ThumbprintList": [THUMBPRINT, IAM_FILLED]},
        ]

        with patch("boto3.client", return_value=iam):
            assert _existing_oidc_provider_thumbprints(manager, "gip-stack") == [THUMBPRINT, IAM_FILLED]


class TestGetStackResourcePhysicalIds:
    def _manager(self, cf_client):
        manager = CloudFormationManager.__new__(CloudFormationManager)
        manager._cf_client = cf_client
        manager.region = "us-east-1"
        return manager

    def test_filters_by_resource_type(self):
        cf = MagicMock()
        cf.describe_stack_resources.return_value = {
            "StackResources": [
                {"ResourceType": "AWS::IAM::OIDCProvider", "PhysicalResourceId": PROVIDER_ARN},
                {"ResourceType": "AWS::IAM::Role", "PhysicalResourceId": "BedrockOktaFederatedRole"},
                {"ResourceType": "AWS::IAM::OIDCProvider"},  # not yet created: no physical id
            ]
        }

        ids = self._manager(cf).get_stack_resource_physical_ids("gip-stack", "AWS::IAM::OIDCProvider")

        assert ids == [PROVIDER_ARN]
        cf.describe_stack_resources.assert_called_once_with(StackName="gip-stack")

    @pytest.mark.parametrize("status", ["DELETE_COMPLETE", "DELETE_IN_PROGRESS", "CREATE_FAILED", "DELETE_FAILED"])
    def test_rows_of_gone_resources_are_ignored(self, status):
        """ROLLBACK_COMPLETE stacks keep reporting their resources as DELETE_COMPLETE with the
        old physical id; those ARNs no longer exist in IAM and must not be looked up."""
        cf = MagicMock()
        cf.describe_stack_resources.return_value = {
            "StackResources": [
                {
                    "ResourceType": "AWS::IAM::OIDCProvider",
                    "PhysicalResourceId": PROVIDER_ARN,
                    "ResourceStatus": status,
                },
            ]
        }

        assert self._manager(cf).get_stack_resource_physical_ids("gip-stack", "AWS::IAM::OIDCProvider") == []

    @pytest.mark.parametrize("status", ["CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_IN_PROGRESS", None])
    def test_rows_of_live_resources_are_returned(self, status):
        cf = MagicMock()
        row = {"ResourceType": "AWS::IAM::OIDCProvider", "PhysicalResourceId": PROVIDER_ARN}
        if status is not None:
            row["ResourceStatus"] = status
        cf.describe_stack_resources.return_value = {"StackResources": [row]}

        assert self._manager(cf).get_stack_resource_physical_ids("gip-stack", "AWS::IAM::OIDCProvider") == [
            PROVIDER_ARN
        ]

    def test_missing_stack_is_empty_not_an_error(self):
        cf = MagicMock()
        cf.describe_stack_resources.side_effect = _client_error("ValidationError", "DescribeStackResources")

        assert self._manager(cf).get_stack_resource_physical_ids("gip-stack", "AWS::IAM::OIDCProvider") == []

    def test_other_errors_propagate(self):
        cf = MagicMock()
        cf.describe_stack_resources.side_effect = _client_error("AccessDenied", "DescribeStackResources")

        with pytest.raises(ClientError):
            self._manager(cf).get_stack_resource_physical_ids("gip-stack", "AWS::IAM::OIDCProvider")
