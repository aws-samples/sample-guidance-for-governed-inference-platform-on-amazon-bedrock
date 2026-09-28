# ABOUTME: Round-trip tests for the model-lifecycle init opt-in and extra_models overlay restore
# ABOUTME: Re-running `gip init` must not silently drop either field (config-sync init round-trip rule)

"""`gip init` round-trip wiring for model lifecycle alerts + extra_models.

`_save_configuration` persists `model_lifecycle_enabled` and `extra_models`
onto the Profile; `_check_existing_deployment` must restore both into the
wizard's config dict so a re-run preserves them (PRs #436/#619/#624 all fixed
fields that were saved but not reloaded).
"""

from unittest.mock import patch

from governed_inference_platform.cli.commands.init import InitCommand
from governed_inference_platform.config import Config, Profile

OVERLAY = {
    "example-9": {
        "name": "Claude Example 9",
        "base_model_id": "anthropic.claude-example-9-v1:0",
        "profiles": {
            "us": {
                "model_id": "us.anthropic.claude-example-9-v1:0",
                "source_regions": ["us-east-1"],
                "destination_regions": ["us-east-1"],
            }
        },
    }
}


def _make_profile(**overrides) -> Profile:
    data = {
        "name": "test",
        "provider_domain": "example.okta.com",
        "client_id": "0oa0000000000000000",
        "identity_pool_name": "gip-auth",
        "credential_storage": "keyring",
        "aws_region": "us-east-1",
    }
    data.update(overrides)
    return Profile(**data)


def _rebuild_config(profile: Profile) -> dict:
    command = InitCommand()
    fake_config = Config()
    with (
        patch.object(Config, "load", return_value=fake_config),
        patch.object(fake_config, "get_profile", return_value=profile),
        patch.object(InitCommand, "_stack_exists", side_effect=Exception("no creds")),
    ):
        return command._check_existing_deployment("test")


class TestModelLifecycleRoundTrip:
    def test_enabled_flag_restored(self):
        rebuilt = _rebuild_config(_make_profile(model_lifecycle_enabled=True))
        assert rebuilt["model_lifecycle"]["enabled"] is True

    def test_disabled_flag_restored(self):
        rebuilt = _rebuild_config(_make_profile(model_lifecycle_enabled=False))
        assert rebuilt["model_lifecycle"]["enabled"] is False


class TestGuardrailsRoundTrip:
    def test_guardrails_fields_restored(self):
        rebuilt = _rebuild_config(
            _make_profile(
                guardrails_enabled=True,
                guardrails_name="team-guardrail",
                guardrails_content_filter_strength="HIGH",
                guardrails_model_include_list=["us.anthropic.claude-sonnet-5"],
                guardrails_kms_key_arn="arn:aws:kms:us-east-1:123456789012:key/00000000-1111-2222-3333-444444444444",
            )
        )

        assert rebuilt["guardrails"] == {
            "enabled": True,
            "name": "team-guardrail",
            "content_filter_strength": "HIGH",
            "model_include_list": ["us.anthropic.claude-sonnet-5"],
            "kms_key_arn": "arn:aws:kms:us-east-1:123456789012:key/00000000-1111-2222-3333-444444444444",
        }


class TestExtraModelsRoundTrip:
    def test_overlay_restored(self):
        rebuilt = _rebuild_config(_make_profile(extra_models=OVERLAY))
        assert rebuilt["extra_models"] == OVERLAY

    def test_empty_overlay_restored(self):
        rebuilt = _rebuild_config(_make_profile())
        assert rebuilt["extra_models"] == {}


class TestSaveConfiguration:
    """_save_configuration maps the wizard dict onto both Profile fields."""

    def test_wizard_fields_include_model_lifecycle_and_extra_models(self):
        """Pin the save-direction mapping (full wizard flow is covered by init e2e tests)."""
        import inspect

        source = inspect.getsource(InitCommand._save_configuration)
        assert '"model_lifecycle_enabled": config_data.get("model_lifecycle", {}).get("enabled", False)' in source
        assert '"guardrails_enabled": config_data.get("guardrails", {}).get("enabled", False)' in source
        assert '"extra_models": config_data.get("extra_models", {})' in source
