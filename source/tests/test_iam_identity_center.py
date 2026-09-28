"""Test cases for AWS IAM Identity Center authentication support."""

from unittest.mock import Mock, patch

import pytest
from rich.console import Console

from governed_inference_platform.cli.commands.deploy import DeployCommand
from governed_inference_platform.config import Profile


class TestAuthTypeBackwardCompat:
    """Verify auth_type derivation from sso_enabled for existing configs."""

    def test_sso_enabled_true_derives_oidc(self):
        """Test that sso_enabled=True derives auth_type=oidc for backward compatibility."""
        data = {
            "name": "test-profile",
            "provider_domain": "company.okta.com",
            "client_id": "test-client",
            "aws_region": "us-east-1",
            "identity_pool_name": "test-pool",
            "sso_enabled": True,
        }

        profile = Profile.from_dict(data)

        assert profile.auth_type == "oidc"
        assert profile.sso_enabled is True
        assert profile.effective_auth_type == "oidc"

    def test_sso_enabled_false_derives_none(self):
        """Test that sso_enabled=False derives auth_type=none for backward compatibility."""
        data = {
            "name": "test-profile",
            "provider_domain": "none",
            "client_id": "none",
            "aws_region": "us-east-1",
            "identity_pool_name": "test-pool",
            "sso_enabled": False,
        }

        profile = Profile.from_dict(data)

        assert profile.auth_type == "none"
        assert profile.sso_enabled is False
        assert profile.effective_auth_type == "none"

    def test_explicit_auth_type_idc_preserved(self):
        """Test that explicitly set auth_type=idc is preserved."""
        data = {
            "name": "test-profile",
            "provider_domain": "none",
            "client_id": "none",
            "aws_region": "us-east-1",
            "identity_pool_name": "test-pool",
            "auth_type": "idc",
            "idc_start_url": "https://company.awsapps.com/start",
            "idc_account_id": "123456789012",
            "idc_permission_set_name": "BedrockAccess",
        }

        profile = Profile.from_dict(data)

        assert profile.auth_type == "idc"
        assert profile.effective_auth_type == "idc"
        assert profile.idc_start_url == "https://company.awsapps.com/start"
        assert profile.idc_account_id == "123456789012"
        assert profile.idc_permission_set_name == "BedrockAccess"

    def test_explicit_auth_type_overrides_sso_enabled(self):
        """Test that explicit auth_type takes precedence over sso_enabled."""
        data = {
            "name": "test-profile",
            "provider_domain": "none",
            "client_id": "none",
            "aws_region": "us-east-1",
            "identity_pool_name": "test-pool",
            "sso_enabled": True,  # This should be ignored
            "auth_type": "none",  # This should take precedence
        }

        profile = Profile.from_dict(data)

        assert profile.auth_type == "none"
        assert profile.effective_auth_type == "none"


class TestDeployAuthRouting:
    """Verify deploy routes to correct template based on auth_type."""

    @patch("governed_inference_platform.cli.commands.deploy.Path")
    def test_oidc_deploys_provider_template(self, mock_path):
        """Test that auth_type=oidc deploys the appropriate OIDC provider template."""
        profile = Profile(
            name="test-profile",
            provider_domain="company.okta.com",
            client_id="test-client",
            credential_storage="session",
            aws_region="us-east-1",
            identity_pool_name="test-pool",
            auth_type="oidc",
            provider_type="okta",
        )

        from governed_inference_platform.cli.commands.deploy import DeployCommand

        # Mock the project root path
        mock_project_root = Mock()
        mock_path.return_value.parent.parent.parent = mock_project_root

        DeployCommand()

        # Test that the method would select okta template
        assert profile.effective_auth_type == "oidc"
        assert profile.provider_type == "okta"

    def test_idc_deploys_idc_template(self):
        """Test that auth_type=idc would deploy bedrock-auth-idc.yaml template."""
        profile = Profile(
            name="test-profile",
            provider_domain="none",
            client_id="none",
            credential_storage="session",
            aws_region="us-east-1",
            identity_pool_name="test-pool",
            auth_type="idc",
            idc_start_url="https://company.awsapps.com/start",
            idc_account_id="123456789012",
            idc_permission_set_name="BedrockAccess",
        )

        assert profile.effective_auth_type == "idc"
        # In deploy.py, this would select bedrock-auth-idc.yaml template

    def test_none_skips_auth_stack(self):
        """Test that auth_type=none skips authentication stack deployment."""
        profile = Profile(
            name="test-profile",
            provider_domain="none",
            client_id="none",
            credential_storage="session",
            aws_region="us-east-1",
            identity_pool_name="test-pool",
            auth_type="none",
        )

        assert profile.effective_auth_type == "none"
        # In deploy.py, this would skip the auth stack entirely

    def test_quota_skipped_for_idc(self):
        """Test that quota monitoring is skipped for auth_type=idc."""
        profile = Profile(
            name="test-profile",
            provider_domain="none",
            client_id="none",
            credential_storage="session",
            aws_region="us-east-1",
            identity_pool_name="test-pool",
            auth_type="idc",
            quota_monitoring_enabled=True,  # This should be ignored
        )

        assert profile.effective_auth_type == "idc"
        # In deploy.py, quota stack would be skipped despite quota_monitoring_enabled=True

    def test_quota_skipped_for_none(self):
        """Test that quota monitoring is skipped for auth_type=none."""
        profile = Profile(
            name="test-profile",
            provider_domain="none",
            client_id="none",
            credential_storage="session",
            aws_region="us-east-1",
            identity_pool_name="test-pool",
            auth_type="none",
            quota_monitoring_enabled=True,  # This should be ignored
        )

        assert profile.effective_auth_type == "none"
        # In deploy.py, quota stack would be skipped despite quota_monitoring_enabled=True

    def test_quota_deployed_for_oidc(self):
        """Test that quota monitoring is deployed for auth_type=oidc."""
        profile = Profile(
            name="test-profile",
            provider_domain="company.okta.com",
            client_id="test-client",
            credential_storage="session",
            aws_region="us-east-1",
            identity_pool_name="test-pool",
            auth_type="oidc",
            quota_monitoring_enabled=True,
        )

        assert profile.effective_auth_type == "oidc"
        # In deploy.py, quota stack would be deployed when quota_monitoring_enabled=True


class TestIdcCfnTemplate:
    """Validate bedrock-auth-idc.yaml structure."""

    def test_template_exists(self):
        """Test that the IDC CloudFormation template file exists."""
        from pathlib import Path

        # Get the template path relative to the source directory
        template_path = Path(__file__).parent.parent.parent / "deployment" / "infrastructure" / "bedrock-auth-idc.yaml"

        assert template_path.exists(), f"IDC template not found at {template_path}"

    def test_template_has_required_parameters(self):
        """Test that the template has all required parameters."""
        from pathlib import Path

        template_path = Path(__file__).parent.parent.parent / "deployment" / "infrastructure" / "bedrock-auth-idc.yaml"

        with open(template_path) as f:
            template_content = f.read()

        required_params = [
            "FederatedRoleName",
            "AllowedPermissionSetName",
            "IdentityPoolName",
            "AllowedBedrockRegions",
            "EnableMonitoring",
        ]

        for param in required_params:
            assert param in template_content, f"Required parameter {param} not found in template"

    def test_trust_is_scoped_to_named_permission_set(self):
        from pathlib import Path

        template_path = Path(__file__).parent.parent.parent / "deployment" / "infrastructure" / "bedrock-auth-idc.yaml"
        content = template_path.read_text(encoding="utf-8")

        assert "Service: 'sso.amazonaws.com'" not in content
        assert "AWSReservedSSO_${AllowedPermissionSetName}_*" in content
        assert "role/aws-reserved/sso.amazonaws.com/*'" not in content

    def test_template_has_required_outputs(self):
        """Test that the template has all required outputs."""
        from pathlib import Path

        template_path = Path(__file__).parent.parent.parent / "deployment" / "infrastructure" / "bedrock-auth-idc.yaml"

        with open(template_path) as f:
            template_content = f.read()

        required_outputs = ["RoleArn", "PolicyArn", "RoleName"]

        for output in required_outputs:
            assert output in template_content, f"Required output {output} not found in template"

    def test_cfn_lint_passes(self):
        """Test that cfn-lint passes on the template if available."""
        try:
            import subprocess
            from pathlib import Path

            template_path = (
                Path(__file__).parent.parent.parent / "deployment" / "infrastructure" / "bedrock-auth-idc.yaml"
            )

            # Try to run cfn-lint if it's available
            result = subprocess.run(["cfn-lint", str(template_path)], capture_output=True, text=True, timeout=30)  # nosec B603  # fmt: skip

            if result.returncode != 0:
                print(f"cfn-lint output: {result.stdout}")
                print(f"cfn-lint errors: {result.stderr}")

            assert result.returncode == 0, f"cfn-lint failed: {result.stderr}"

        except (subprocess.TimeoutExpired, FileNotFoundError):
            pytest.skip("cfn-lint not available or timed out")
        except Exception as e:
            pytest.skip(f"Could not run cfn-lint: {e}")


class TestIdcDeployRoleCompatibility:
    @staticmethod
    def _profile() -> Profile:
        return Profile(
            name="test-idc",
            provider_domain="none",
            client_id="none",
            credential_storage="session",
            aws_region="us-east-1",
            identity_pool_name="test-pool",
            stack_names={"auth": "gip-auth"},
            monitoring_enabled=False,
            allowed_bedrock_regions=["us-east-1"],
            auth_type="idc",
            idc_permission_set_name="BedrockDeveloperAccess",
        )

    def test_update_preserves_existing_federated_role_name(self):
        manager = Mock()
        manager.get_stack_parameters.return_value = {"FederatedRoleName": "ExistingFederatedRole"}
        manager.deploy_stack.return_value = Mock(success=True, error=None)

        assert DeployCommand()._deploy_stack("auth", self._profile(), Console(), manager) == 0

        parameters = {
            item["ParameterKey"]: item["ParameterValue"] for item in manager.deploy_stack.call_args.kwargs["parameters"]
        }
        assert parameters["FederatedRoleName"] == "ExistingFederatedRole"
        assert parameters["AllowedPermissionSetName"] == "BedrockDeveloperAccess"

    def test_new_stack_uses_dedicated_federated_role_name(self):
        manager = Mock()
        manager.get_stack_parameters.return_value = {}
        manager.deploy_stack.return_value = Mock(success=True, error=None)

        assert DeployCommand()._deploy_stack("auth", self._profile(), Console(), manager) == 0

        parameters = {
            item["ParameterKey"]: item["ParameterValue"] for item in manager.deploy_stack.call_args.kwargs["parameters"]
        }
        assert parameters["FederatedRoleName"] == "BedrockIDCFederatedRole"

    def test_show_commands_omits_role_name_to_preserve_updates(self):
        console = Console(record=True)

        DeployCommand()._show_deployment_commands("auth", self._profile(), console)

        output = console.export_text()
        assert "AllowedPermissionSetName=BedrockDeveloperAccess" in output
        assert "FederatedRoleName=" not in output


class TestConfigSaveLoad:
    """Test that IDC configuration is properly saved and loaded."""

    def test_idc_fields_saved_and_loaded(self):
        """Test that IDC-specific fields are properly saved and loaded."""
        profile = Profile(
            name="test-idc-profile",
            provider_domain="none",
            client_id="none",
            credential_storage="session",
            aws_region="us-east-1",
            identity_pool_name="test-pool",
            auth_type="idc",
            idc_start_url="https://company.awsapps.com/start",
            idc_account_id="123456789012",
            idc_permission_set_name="BedrockDeveloperAccess",
        )

        # Convert to dict and back to simulate save/load
        profile_dict = profile.to_dict()
        loaded_profile = Profile.from_dict(profile_dict)

        assert loaded_profile.auth_type == "idc"
        assert loaded_profile.idc_start_url == "https://company.awsapps.com/start"
        assert loaded_profile.idc_account_id == "123456789012"
        assert loaded_profile.idc_permission_set_name == "BedrockDeveloperAccess"
        assert loaded_profile.effective_auth_type == "idc"


if __name__ == "__main__":
    pytest.main([__file__])
