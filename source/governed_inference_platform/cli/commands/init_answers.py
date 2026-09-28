# ABOUTME: Non-interactive/GitOps mode for `gip init` — answers-file loading, defaults, validation, export
# ABOUTME: Schema mirrors the wizard's internal config dict (the exact keys _save_configuration reads)

"""Non-interactive `gip init --from-file` / `--export-answers` support.

The answers file is a YAML or JSON document whose structure mirrors the
internal wizard config dict — the same keys the interactive wizard writes and
``InitCommand._save_configuration`` reads. Values omitted from the file take
the same defaults as pressing Enter through the wizard for a minimal OIDC
deployment. Two fields have no possible default and must always be provided
for OIDC deployments: ``okta.domain`` and ``okta.client_id``.

No questionary prompt is ever issued on this path (CI has no TTY).
"""

import copy
import json
import re
import uuid
from pathlib import Path
from typing import Any

import yaml
from rich.console import Console

from governed_inference_platform.cli.utils.validators import (
    validate_aws_region,
    validate_client_id,
    validate_oidc_provider_domain,
)
from governed_inference_platform.config import IAM_OIDC_THUMBPRINT_GUIDE_URL, Config

DEFAULT_MODEL_KEY = "sonnet-4-5"

# Auth methods and OIDC provider types the wizard offers. Single source of
# truth for validate_config below and for the `gip console` bootstrap catalog
# (do not re-declare these literals elsewhere).
VALID_AUTH_TYPES = ("oidc", "idc", "none")
VALID_PROVIDER_TYPES = ("okta", "auth0", "azure", "cognito", "google", "generic")

# Sentinel for dicts with free-form keys (tags, cowork_3p.extra_keys)
_ANY = "__any__"

# Allowed answers-file structure. Leaves are None; nested dicts list their
# allowed keys; _ANY marks free-form mappings. Anything else is an unknown key
# (typo protection — hard error).
ALLOWED_KEYS: dict[str, Any] = {
    "auth_type": None,
    "sso_enabled": None,
    "okta": {"domain": None, "client_id": None},
    "provider_type": None,
    "cognito_user_pool_id": None,
    "oidc_issuer_url": None,
    "oidc_authorization_endpoint": None,
    "oidc_token_endpoint": None,
    "oidc_jwks_uri": None,
    "oidc_thumbprint": None,
    "azure_auth_mode": None,
    "client_certificate_path": None,
    "client_certificate_key_path": None,
    "credential_storage": None,
    "redirect_port": None,
    "federation_type": None,
    "max_session_duration": None,
    "session_name_binding": None,
    "idc_start_url": None,
    "idc_account_id": None,
    "idc_permission_set_name": None,
    "sso_region": None,
    "aws": {
        "region": None,
        "identity_pool_name": None,
        "stacks": {"auth": None, "monitoring": None, "dashboard": None, "analytics": None},
        "allowed_bedrock_regions": None,
        "cross_region_profile": None,
        "selected_model": None,
        "model_alias": None,
        "selected_source_region": None,
        "inference_profile_opus_arn": None,
        "inference_profile_sonnet_arn": None,
        "inference_profile_haiku_arn": None,
    },
    "monitoring": {
        "enabled": None,
        "mode": None,
        "vpc_config": {
            "create_vpc": None,
            "vpc_id": None,
            "subnet_ids": None,
            "vpc_cidr": None,
            "subnet1_cidr": None,
            "subnet2_cidr": None,
        },
        "alb_scheme": None,
        "custom_domain": None,
        "hosted_zone_id": None,
        "allow_insecure_http_ingress": False,
    },
    "analytics": {"enabled": None},
    "quota": {
        "enabled": None,
        "limit_type": None,
        "monthly_cost_limit": None,
        "daily_cost_limit": None,
        "monthly_limit": None,
        "daily_limit": None,
        "warning_threshold_80": None,
        "warning_threshold_90": None,
        "burst_buffer_percent": None,
        "daily_enforcement_mode": None,
        "monthly_enforcement_mode": None,
        "check_interval": None,
        "enable_bypass_detection": None,
    },
    "metering": {"enabled": None, "mode": None},
    "codebuild": {"enabled": None, "region": None, "prior_regions": None},
    "web_search": {"enabled": None, "entitled_groups": None, "policy_mode": None},
    "memory": {
        "enabled": None,
        "user_enabled": None,
        "org_enabled": None,
        "mode": None,
        "raw_event_retention_days": None,
        "org_write_groups": None,
    },
    "skills": {
        "enabled": None,
        "publisher_groups": None,
        "curator_groups": None,
        "organization_id": None,
        "registry_name": None,
    },
    "model_lifecycle": {"enabled": None},
    "guardrails": {
        "enabled": None,
        "name": None,
        "content_filter_strength": None,
        "model_include_list": None,
        "kms_key_arn": None,
    },
    "cowork_3p": {
        "enabled": None,
        "extra_keys": _ANY,
        "service_token": None,
        "chat_tab_enabled": None,
        "chat_advanced_file_analysis": None,
    },
    "cowork": {"config_delivery": None},
    "settings_target": None,
    "lock_default_model": None,
    "distribution": {
        "enabled": None,
        "type": None,
        "idp_provider": None,
        "idp_domain": None,
        "idp_client_id": None,
        "idp_client_secret_arn": None,
        "custom_domain": None,
        "hosted_zone_id": None,
        "idp_issuer": None,
        "idp_authorization_endpoint": None,
        "idp_token_endpoint": None,
        "idp_userinfo_endpoint": None,
    },
    "tags": _ANY,
    "extra_files": None,
    "extra_models": _ANY,
}

# Raw secrets must never live in a GitOps answers file.
FORBIDDEN_KEYS: dict[str, str] = {
    "client_secret": (
        "Azure confidential-client secrets cannot come from the answers file. "
        "Store the secret in the OS keyring instead: set azure_auth_mode: secret in the file, then run "
        "'credential-process --set-client-secret --profile <profile>' on each machine."
    ),
    "okta.client_secret": (
        "Client secrets cannot come from the answers file. Use azure_auth_mode: secret and store the "
        "secret in the OS keyring via 'credential-process --set-client-secret --profile <profile>'."
    ),
    "distribution.idp_client_secret": (
        "Raw IdP client secrets cannot come from the answers file. Pre-create the secret in AWS Secrets "
        "Manager and reference it via distribution.idp_client_secret_arn."
    ),
}

# Defaults equal to pressing Enter through the wizard for a minimal OIDC
# deployment. None values are either "no value" (same as the wizard) or are
# derived after merge in _apply_derivations().
DEFAULTS: dict[str, Any] = {
    "auth_type": "oidc",
    "okta": {"domain": None, "client_id": None},  # REQUIRED for OIDC — no default possible
    "provider_type": None,  # derived from okta.domain
    "cognito_user_pool_id": None,
    "oidc_issuer_url": None,
    "oidc_authorization_endpoint": None,
    "oidc_token_endpoint": None,
    "oidc_jwks_uri": None,
    "oidc_thumbprint": None,
    "azure_auth_mode": None,  # derived: "public" when provider_type == "azure"
    "client_certificate_path": None,
    "client_certificate_key_path": None,
    "credential_storage": "session",
    "redirect_port": None,
    "federation_type": "direct",
    "max_session_duration": None,  # derived: 43200 direct / 28800 cognito
    "session_name_binding": "none",  # opt-in sts:RoleSessionName trust-policy binding (ADR-0015)
    "idc_start_url": None,
    "idc_account_id": None,
    "idc_permission_set_name": None,  # derived: "BedrockDeveloperAccess" for IDC
    "sso_region": None,  # derived from idc_start_url for IDC
    "aws": {
        "region": "us-east-1",
        "identity_pool_name": "gip-auth",
        "stacks": None,  # derived from identity_pool_name
        "allowed_bedrock_regions": None,  # derived from model + cross-region profile
        "cross_region_profile": None,  # derived (first available for the model, "us" for sonnet-4-5)
        "selected_model": None,  # derived (sonnet-4-5 for the chosen profile)
        "model_alias": None,
        "selected_source_region": None,  # derived (first available source region)
        "inference_profile_opus_arn": None,
        "inference_profile_sonnet_arn": None,
        "inference_profile_haiku_arn": None,
    },
    "monitoring": {
        "enabled": True,
        "mode": "sidecar",
        "vpc_config": None,  # derived: {"create_vpc": True} for central mode
        "alb_scheme": None,  # derived: "internet-facing" for central mode
        "custom_domain": None,
        "hosted_zone_id": None,
        "allow_insecure_http_ingress": False,
    },
    "analytics": {"enabled": None},  # derived: central → True, sidecar → False
    "quota": {
        "enabled": None,  # derived: True for OIDC + monitoring, False for IDC/none
        "limit_type": "cost",
        "monthly_cost_limit": None,  # derived: 50.0 in cost mode, 0 in token mode
        "daily_cost_limit": None,  # derived: 0 in cost mode
        "monthly_limit": None,  # derived: 0 in cost mode, 225M in token mode
        "daily_limit": None,  # derived from monthly + burst buffer in token mode
        "warning_threshold_80": None,  # derived: 80% of monthly in token mode
        "warning_threshold_90": None,  # derived: 90% of monthly in token mode
        "burst_buffer_percent": 10,
        "daily_enforcement_mode": "alert",
        "monthly_enforcement_mode": "block",
        "check_interval": 30,
        "enable_bypass_detection": False,
    },
    "metering": {"enabled": False, "mode": "shadow"},
    "codebuild": {"enabled": False, "region": None, "prior_regions": []},
    "web_search": {"enabled": False, "entitled_groups": [], "policy_mode": "LOG_ONLY"},
    "memory": {
        "enabled": None,  # derived: user_enabled or org_enabled
        "user_enabled": False,
        "org_enabled": False,
        "mode": "extracted-only",
        "raw_event_retention_days": 30,
        "org_write_groups": [],
    },
    "skills": {
        "enabled": False,
        "publisher_groups": [],
        "curator_groups": [],
        "organization_id": "",
        "registry_name": "gip-skills",
    },
    "model_lifecycle": {"enabled": False},
    "guardrails": {
        "enabled": False,
        "name": "",
        "content_filter_strength": "MEDIUM",
        "model_include_list": [],
        "kms_key_arn": None,
    },
    "cowork_3p": {
        "enabled": True,
        "extra_keys": {},
        "service_token": "",  # derived: uuid4 for central monitoring mode (wizard behavior)
        "chat_tab_enabled": True,
        "chat_advanced_file_analysis": True,
    },
    "cowork": {"config_delivery": "static"},
    "settings_target": "user",
    "lock_default_model": False,
    "distribution": {
        "enabled": False,
        "type": None,
        "idp_provider": None,
        "idp_domain": None,
        "idp_client_id": None,
        "idp_client_secret_arn": None,
        "custom_domain": None,
        "hosted_zone_id": None,
        "idp_issuer": None,
        "idp_authorization_endpoint": None,
        "idp_token_endpoint": None,
        "idp_userinfo_endpoint": None,
    },
    "tags": {},
    "extra_files": [],
    "extra_models": {},
}


def load_answers_file(path: Path) -> dict[str, Any]:
    """Load a YAML or JSON answers file into a dict.

    Raises:
        ValueError: If the file can't be parsed or isn't a mapping.
    """
    if not path.exists():
        raise ValueError(f"Answers file not found: {path}")

    text = path.read_text(encoding="utf-8")
    try:
        if path.suffix.lower() == ".json":
            data = json.loads(text)
        else:
            data = yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as e:
        raise ValueError(f"Could not parse answers file {path}: {e}") from e

    if not isinstance(data, dict):
        raise ValueError(f"Answers file {path} must contain a mapping at the top level")
    return data


def check_structure(answers: dict[str, Any]) -> list[str]:
    """Check the answers dict for unknown and forbidden keys.

    Returns a list of error strings (empty when the structure is clean).
    Unknown keys are a hard error (typo protection); forbidden keys carry
    an explanatory message (secrets never come from the file).
    """
    errors: list[str] = []

    def walk(node: dict[str, Any], allowed: dict[str, Any], prefix: str) -> None:
        for key, value in node.items():
            dotted = f"{prefix}{key}"
            if dotted in FORBIDDEN_KEYS:
                errors.append(f"{dotted}: {FORBIDDEN_KEYS[dotted]}")
                continue
            if key not in allowed:
                errors.append(f"unknown key: {dotted}")
                continue
            sub_allowed = allowed[key]
            if isinstance(sub_allowed, dict) and isinstance(value, dict):
                walk(value, sub_allowed, f"{dotted}.")
            elif isinstance(sub_allowed, dict) and value is not None and not isinstance(value, dict):
                errors.append(f"{dotted}: expected a mapping, got {type(value).__name__}")
            # _ANY (free-form) and leaves need no key checking

    walk(answers, ALLOWED_KEYS, "")
    return errors


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge override into a copy of base. Lists and scalars replace."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _detect_provider_type(domain: str) -> str | None:
    """Auto-detect the IdP type from the provider domain (mirrors the wizard)."""
    from urllib.parse import urlparse

    url = domain if domain.startswith(("http://", "https://")) else f"https://{domain}"
    try:
        hostname = urlparse(url).hostname
    except Exception:
        return None
    if not hostname:
        return None
    h = hostname.lower()
    if h.endswith(".okta.com") or h == "okta.com" or h.endswith(".oktapreview.com") or h.endswith(".okta-emea.com"):
        return "okta"
    if h.endswith(".auth0.com") or h == "auth0.com":
        return "auth0"
    if h.endswith(".microsoftonline.com") or h == "microsoftonline.com":
        return "azure"
    if h.endswith(".windows.net") or h == "windows.net":
        return "azure"
    if h.endswith(".amazoncognito.com") or h == "amazoncognito.com":
        return "cognito"
    if h.startswith("cognito-idp.") and ".amazonaws.com" in h:
        return "cognito"
    if h == "accounts.google.com":
        return "google"
    return None


def _resolve_model(config: dict[str, Any], errors: list[str]) -> None:
    """Resolve model / cross-region profile / regions, mirroring the wizard defaults."""
    from governed_inference_platform.models import (
        CLAUDE_MODELS,
        expand_region_sentinels,
        get_available_profiles_for_model,
        get_destination_regions_for_model_profile,
        get_model_id_for_profile,
        get_source_regions_for_model_profile,
    )

    aws = config["aws"]
    selected_model = aws.get("selected_model")
    cross_region_profile = aws.get("cross_region_profile")

    model_key = None
    profile_key = None
    if selected_model:
        for key, model_info in CLAUDE_MODELS.items():
            for pk in get_available_profiles_for_model(key):
                if model_info["profiles"][pk]["model_id"] == selected_model:
                    model_key, profile_key = key, pk
                    break
            if model_key:
                break
        if not model_key:
            errors.append(f"aws.selected_model: unknown model ID '{selected_model}'")
            return
        if cross_region_profile and cross_region_profile != profile_key:
            errors.append(
                f"aws.cross_region_profile: '{cross_region_profile}' does not match "
                f"aws.selected_model '{selected_model}' (which is the '{profile_key}' profile ID)"
            )
            return
    else:
        model_key = DEFAULT_MODEL_KEY
        available = get_available_profiles_for_model(model_key)
        profile_key = cross_region_profile or available[0]
        if profile_key not in available:
            errors.append(
                f"aws.cross_region_profile: '{profile_key}' not available for the default model "
                f"'{model_key}' (available: {', '.join(available)}); set aws.selected_model explicitly"
            )
            return
        selected_model = get_model_id_for_profile(model_key, profile_key)

    aws["selected_model"] = selected_model
    aws["cross_region_profile"] = profile_key

    if not aws.get("allowed_bedrock_regions"):
        aws["allowed_bedrock_regions"] = list(get_destination_regions_for_model_profile(model_key, profile_key))
    # Expand "all-*" sentinels (global-CRIS catalog entries) into real regions
    # before validate_config runs — keeps parity with the interactive wizard,
    # which performs the same expansion at assignment time.
    if isinstance(aws.get("allowed_bedrock_regions"), list):
        aws["allowed_bedrock_regions"] = expand_region_sentinels([str(r) for r in aws["allowed_bedrock_regions"]])

    source_regions = list(get_source_regions_for_model_profile(model_key, profile_key))
    if aws.get("selected_source_region"):
        if source_regions and aws["selected_source_region"] not in source_regions:
            errors.append(
                f"aws.selected_source_region: '{aws['selected_source_region']}' not valid for "
                f"{model_key}/{profile_key} (valid: {', '.join(source_regions)})"
            )
    else:
        aws["selected_source_region"] = source_regions[0] if source_regions else None


def _apply_derivations(config: dict[str, Any], errors: list[str]) -> None:
    """Fill in wizard-equivalent derived values after the merge."""
    auth_type = config.get("auth_type")

    # sso_enabled follows auth_type exactly (as the wizard sets it)
    derived_sso = auth_type == "oidc"
    if "sso_enabled" in config and config["sso_enabled"] not in (None, derived_sso):
        errors.append(f"sso_enabled: must be {derived_sso} when auth_type is '{auth_type}' (or omit it)")
    config["sso_enabled"] = derived_sso

    if auth_type == "oidc":
        domain = config.get("okta", {}).get("domain")
        if domain:
            domain = domain.replace("https://", "").replace("http://", "").strip("/")
            # GovCloud Cognito domains must use the FIPS endpoint (wizard auto-correct)
            m = re.search(r"\.auth\.([^.]+)\.amazoncognito\.com", domain)
            if m and m.group(1).startswith("us-gov-"):
                domain = domain.replace(".auth.", ".auth-fips.")
            config["okta"]["domain"] = domain
            if not config.get("provider_type"):
                config["provider_type"] = _detect_provider_type(domain)
        if config.get("provider_type") == "azure" and not config.get("azure_auth_mode"):
            config["azure_auth_mode"] = "public"
        if config.get("oidc_thumbprint"):
            config["oidc_thumbprint"] = config["oidc_thumbprint"].strip().replace(":", "").lower()
        if config.get("idc_start_url"):
            config["idc_start_url"] = config["idc_start_url"].strip().rstrip("/")
    elif auth_type == "idc":
        if config.get("idc_start_url"):
            config["idc_start_url"] = config["idc_start_url"].strip().rstrip("/")
            if not config.get("sso_region"):
                m = re.search(r"\.(us|eu|ap|sa|ca|me|af|il)-[a-z]+-\d+\.", config["idc_start_url"])
                config["sso_region"] = m.group(0).strip(".") if m else "us-east-1"
        if not config.get("idc_permission_set_name"):
            config["idc_permission_set_name"] = "BedrockDeveloperAccess"

    if config.get("max_session_duration") is None:
        config["max_session_duration"] = 43200 if config.get("federation_type") == "direct" else 28800

    # Stack names derive from the identity pool / stack base name
    pool_name = config["aws"].get("identity_pool_name")
    stacks = config["aws"].get("stacks") or {}
    derived_stacks = {
        "auth": f"{pool_name}-stack",
        "monitoring": f"{pool_name}-monitoring",
        "dashboard": f"{pool_name}-dashboard",
        "analytics": f"{pool_name}-analytics",
    }
    config["aws"]["stacks"] = {**derived_stacks, **{k: v for k, v in stacks.items() if v}}

    _resolve_model(config, errors)

    # Monitoring mode consequences (wizard forces these per mode)
    monitoring = config["monitoring"]
    if monitoring.get("enabled"):
        if monitoring.get("mode") == "central":
            if not monitoring.get("vpc_config"):
                monitoring["vpc_config"] = {"create_vpc": True}
            if not monitoring.get("alb_scheme"):
                monitoring["alb_scheme"] = "internet-facing"
            if config["analytics"].get("enabled") is None:
                config["analytics"]["enabled"] = True
        else:
            monitoring["vpc_config"] = None
            monitoring["custom_domain"] = None
            monitoring["hosted_zone_id"] = None
            monitoring["allow_insecure_http_ingress"] = False
            config["analytics"]["enabled"] = False
    else:
        config["analytics"]["enabled"] = False

    # Quota derivations (wizard: OIDC defaults to enabled; IDC defaults to
    # disabled; anonymous auth cannot enforce quotas at all)
    quota = config["quota"]
    if quota.get("enabled") is None:
        quota["enabled"] = bool(auth_type == "oidc" and monitoring.get("enabled"))
    if auth_type == "none":
        if quota["enabled"]:
            errors.append(
                "quota.enabled: quota enforcement requires per-user identity — not available with auth_type 'none'"
            )
        quota["enabled"] = False
    if not monitoring.get("enabled"):
        quota["enabled"] = False

    if quota.get("limit_type") == "cost":
        if quota.get("monthly_cost_limit") is None:
            quota["monthly_cost_limit"] = 50.0
        if quota.get("daily_cost_limit") is None:
            quota["daily_cost_limit"] = 0
        # Cost mode zeroes token limits (wizard behavior)
        quota["monthly_limit"] = 0
        quota["daily_limit"] = 0
        quota["warning_threshold_80"] = 0
        quota["warning_threshold_90"] = 0
    elif quota.get("limit_type") == "token":
        quota["monthly_cost_limit"] = 0
        quota["daily_cost_limit"] = 0
        if quota.get("monthly_limit") is None:
            quota["monthly_limit"] = 225_000_000
        monthly = quota["monthly_limit"]
        if isinstance(monthly, int) and monthly > 0:
            if quota.get("warning_threshold_80") is None:
                quota["warning_threshold_80"] = int(monthly * 0.8)
            if quota.get("warning_threshold_90") is None:
                quota["warning_threshold_90"] = int(monthly * 0.9)
            burst = quota.get("burst_buffer_percent")
            if quota.get("daily_limit") is None and isinstance(burst, int):
                quota["daily_limit"] = int(monthly / 30 * (1 + burst / 100))

    # Bypass detection only exists in sidecar mode (central runs server-side)
    if monitoring.get("mode") != "sidecar":
        quota["enable_bypass_detection"] = False

    # CodeBuild region resolution mirrors the wizard's supported-region logic
    codebuild = config["codebuild"]
    if codebuild.get("enabled"):
        from governed_inference_platform.cli.utils.helpers import CODEBUILD_WINDOWS_REGIONS

        main_region = config["aws"].get("region")
        cb_region = codebuild.get("region")
        if cb_region:
            if cb_region not in CODEBUILD_WINDOWS_REGIONS:
                errors.append(
                    f"codebuild.region: '{cb_region}' does not support Windows CodeBuild containers "
                    f"(supported: {', '.join(sorted(CODEBUILD_WINDOWS_REGIONS))})"
                )
        elif main_region not in CODEBUILD_WINDOWS_REGIONS:
            errors.append(
                f"codebuild.region: required — Windows CodeBuild containers are not available in "
                f"'{main_region}' (supported: {', '.join(sorted(CODEBUILD_WINDOWS_REGIONS))})"
            )

    # Web search is not available for IDC (wizard skips the prompt)
    if auth_type == "idc":
        if config["web_search"].get("enabled"):
            errors.append("web_search.enabled: web search is not available for IAM Identity Center deployments")
        config["web_search"]["enabled"] = False

    # Memory: enabled follows the scope flags (wizard behavior); it requires
    # the web search gateway to attach to (ADR-0016).
    memory = config["memory"]
    scopes_on = bool(memory.get("user_enabled") or memory.get("org_enabled"))
    if memory.get("enabled") and not scopes_on:
        errors.append(
            "memory.enabled: enable at least one of memory.user_enabled / memory.org_enabled (or omit enabled)"
        )
    memory["enabled"] = scopes_on
    if memory["enabled"] and not config["web_search"].get("enabled"):
        errors.append(
            "memory.user_enabled/org_enabled: memory requires web_search.enabled "
            "(the memory tools attach to the web search gateway)"
        )

    # Claude Desktop service token for central-mode ALB auth bypass (wizard generates one)
    cowork_3p = config["cowork_3p"]
    if cowork_3p.get("enabled") and monitoring.get("mode") == "central" and not cowork_3p.get("service_token"):
        cowork_3p["service_token"] = str(uuid.uuid4())

    # Distribution enabled flag follows type (wizard sets enabled from the type choice)
    distribution = config["distribution"]
    if distribution.get("enabled") and not distribution.get("type"):
        errors.append(
            "distribution.type: required when distribution.enabled is true ('presigned-s3' or 'landing-page')"
        )
    distribution["enabled"] = distribution.get("type") is not None


def validate_config(config: dict[str, Any]) -> list[str]:
    """Validate the merged config with the same rules the wizard applies.

    Returns ALL problems found (never just the first).
    """
    # Imported here (not at module top) to avoid a circular import with init.py
    from governed_inference_platform.cli.commands.init import (
        validate_cognito_user_pool_id,
        validate_identity_pool_name,
    )
    from governed_inference_platform.validators import ProfileValidator

    errors: list[str] = []
    auth_type = config.get("auth_type")

    if auth_type not in VALID_AUTH_TYPES:
        errors.append(f"auth_type: must be one of oidc, idc, none (got '{auth_type}')")

    if auth_type == "oidc":
        domain = config.get("okta", {}).get("domain")
        client_id = config.get("okta", {}).get("client_id")
        if not domain:
            errors.append("okta.domain: required for auth_type 'oidc' (no default possible)")
        elif not validate_oidc_provider_domain(domain):
            errors.append(f"okta.domain: invalid provider domain format '{domain}' (e.g. company.okta.com)")
        if not client_id:
            errors.append("okta.client_id: required for auth_type 'oidc' (no default possible)")
        elif not validate_client_id(str(client_id)):
            errors.append("okta.client_id: invalid client ID (must be at least 10 characters, alphanumeric)")

        provider_type = config.get("provider_type")
        valid_providers = VALID_PROVIDER_TYPES
        if domain and not provider_type:
            errors.append(
                "provider_type: could not auto-detect from okta.domain — set it explicitly "
                f"(one of: {', '.join(valid_providers)})"
            )
        elif provider_type and provider_type not in valid_providers:
            errors.append(f"provider_type: must be one of {', '.join(valid_providers)} (got '{provider_type}')")

        if provider_type == "cognito":
            pool_id = config.get("cognito_user_pool_id")
            if not pool_id:
                errors.append("cognito_user_pool_id: required when provider_type is 'cognito'")
            elif validate_cognito_user_pool_id(pool_id) is not True:
                errors.append(f"cognito_user_pool_id: invalid User Pool ID format '{pool_id}'")

        if provider_type == "generic":
            for field in (
                "oidc_issuer_url",
                "oidc_authorization_endpoint",
                "oidc_token_endpoint",
                "oidc_jwks_uri",
            ):
                if not config.get(field):
                    errors.append(f"{field}: required when provider_type is 'generic' (no OIDC discovery in CI)")
            issuer = config.get("oidc_issuer_url")
            if issuer and not issuer.startswith("https://"):
                errors.append("oidc_issuer_url: must start with https://")
            # oidc_thumbprint is optional: IAM retrieves the CA thumbprint itself when the
            # template omits ThumbprintList. Validate the format only when a value is given.
            thumbprint = config.get("oidc_thumbprint")
            if thumbprint and not re.fullmatch(r"[0-9a-f]{40}", thumbprint):
                errors.append(
                    "oidc_thumbprint: must be 40 hex characters (colons optional); leave unset unless the "
                    f"JWKS host uses a private CA, see {IAM_OIDC_THUMBPRINT_GUIDE_URL}"
                )

        azure_mode = config.get("azure_auth_mode")
        if azure_mode and azure_mode not in ("public", "secret", "certificate"):
            errors.append(f"azure_auth_mode: must be one of public, secret, certificate (got '{azure_mode}')")
        if azure_mode == "certificate":
            if not config.get("client_certificate_path"):
                errors.append("client_certificate_path: required when azure_auth_mode is 'certificate'")
            if not config.get("client_certificate_key_path"):
                errors.append("client_certificate_key_path: required when azure_auth_mode is 'certificate'")

    if auth_type == "idc":
        if not config.get("idc_start_url"):
            errors.append("idc_start_url: required for auth_type 'idc' (e.g. https://company.awsapps.com/start)")
        account_id = config.get("idc_account_id")
        if not account_id:
            errors.append("idc_account_id: required for auth_type 'idc' (12-digit AWS account ID)")
        elif not (len(str(account_id).strip()) == 12 and str(account_id).strip().isdigit()):
            errors.append(f"idc_account_id: must be a 12-digit AWS account ID (got '{account_id}')")

    if config.get("credential_storage") not in ("keyring", "session"):
        errors.append(f"credential_storage: must be 'keyring' or 'session' (got '{config.get('credential_storage')}')")

    redirect_port = config.get("redirect_port")
    if redirect_port is not None and not (isinstance(redirect_port, int) and 1024 <= redirect_port <= 65535):
        errors.append(f"redirect_port: must be a number between 1024 and 65535 (got '{redirect_port}')")

    if config.get("federation_type") not in ("direct", "cognito"):
        errors.append(f"federation_type: must be 'direct' or 'cognito' (got '{config.get('federation_type')}')")

    duration = config.get("max_session_duration")
    if not (isinstance(duration, int) and 3600 <= duration <= 43200):
        errors.append(f"max_session_duration: must be a number between 3600 and 43200 (got '{duration}')")

    binding = config.get("session_name_binding")
    if binding not in ("none", "email", "sub"):
        errors.append(f"session_name_binding: must be 'none', 'email', or 'sub' (got '{binding}')")
    elif binding == "sub" and config.get("provider_type") == "auth0":
        errors.append(
            "session_name_binding: 'sub' is not supported for Auth0 (Auth0 sub claims "
            "contain '|', which STS forbids in session names) — use 'email' or 'none'"
        )

    aws = config.get("aws", {})
    region = aws.get("region")
    if not region or not validate_aws_region(str(region).replace("us-gov-", "us-")):
        errors.append(f"aws.region: invalid AWS region '{region}'")
    pool_name = aws.get("identity_pool_name")
    pool_result = validate_identity_pool_name(pool_name or "")
    if pool_result is not True:
        errors.append(f"aws.identity_pool_name: {pool_result}")
    regions = aws.get("allowed_bedrock_regions")
    if regions is not None:
        if not isinstance(regions, list) or not all(
            validate_aws_region(str(r).replace("us-gov-", "us-")) for r in regions
        ):
            errors.append(f"aws.allowed_bedrock_regions: invalid region list {regions}")

    for arn_key in ("inference_profile_opus_arn", "inference_profile_sonnet_arn", "inference_profile_haiku_arn"):
        arn = aws.get(arn_key)
        if arn:
            arn_error = ProfileValidator.validate_application_inference_profile_arn(arn)
            if arn_error:
                errors.append(f"aws.{arn_key}: {arn_error}")

    monitoring = config.get("monitoring", {})
    if not isinstance(monitoring.get("enabled"), bool):
        errors.append(f"monitoring.enabled: must be true or false (got '{monitoring.get('enabled')}')")
    if monitoring.get("mode") not in ("sidecar", "central"):
        errors.append(f"monitoring.mode: must be 'sidecar' or 'central' (got '{monitoring.get('mode')}')")
    if monitoring.get("alb_scheme") not in (None, "internet-facing", "internal"):
        errors.append(
            f"monitoring.alb_scheme: must be 'internet-facing' or 'internal' (got '{monitoring.get('alb_scheme')}')"
        )
    allow_insecure_http = monitoring.get("allow_insecure_http_ingress", False)
    if not isinstance(allow_insecure_http, bool):
        errors.append(f"monitoring.allow_insecure_http_ingress: must be true or false (got '{allow_insecure_http}')")
    if monitoring.get("enabled") and monitoring.get("mode") == "central":
        if allow_insecure_http and monitoring.get("alb_scheme") != "internal":
            errors.append(
                "monitoring.allow_insecure_http_ingress: local-development exception requires alb_scheme 'internal'"
            )
        if not monitoring.get("custom_domain") and not allow_insecure_http:
            errors.append(
                "monitoring.custom_domain: central monitoring requires HTTPS; configure a custom domain or set "
                "allow_insecure_http_ingress=true for internal local development only"
            )
    vpc_config = monitoring.get("vpc_config")
    if vpc_config and not vpc_config.get("create_vpc"):
        if not vpc_config.get("vpc_id"):
            errors.append("monitoring.vpc_config.vpc_id: required when create_vpc is false")
        if not vpc_config.get("subnet_ids"):
            errors.append("monitoring.vpc_config.subnet_ids: required when create_vpc is false")

    quota = config.get("quota", {})
    if quota.get("limit_type") not in ("cost", "token"):
        errors.append(f"quota.limit_type: must be 'cost' or 'token' (got '{quota.get('limit_type')}')")
    for mode_key in ("daily_enforcement_mode", "monthly_enforcement_mode"):
        if quota.get(mode_key) not in ("alert", "block"):
            errors.append(f"quota.{mode_key}: must be 'alert' or 'block' (got '{quota.get(mode_key)}')")
    burst = quota.get("burst_buffer_percent")
    if not (isinstance(burst, int) and 5 <= burst <= 25):
        errors.append(f"quota.burst_buffer_percent: must be a number between 5 and 25 (got '{burst}')")
    interval = quota.get("check_interval")
    if not (isinstance(interval, int) and interval >= 0):
        errors.append(f"quota.check_interval: must be a non-negative number of minutes (got '{interval}')")
    if quota.get("enabled") and quota.get("limit_type") == "cost":
        monthly_cost = quota.get("monthly_cost_limit")
        if not (isinstance(monthly_cost, (int, float)) and monthly_cost > 0):
            errors.append(f"quota.monthly_cost_limit: must be a positive number in cost mode (got '{monthly_cost}')")
        daily_cost = quota.get("daily_cost_limit")
        if not (isinstance(daily_cost, (int, float)) and daily_cost >= 0):
            errors.append(f"quota.daily_cost_limit: must be a non-negative number (got '{daily_cost}')")
    if quota.get("enabled") and quota.get("limit_type") == "token":
        monthly = quota.get("monthly_limit")
        if not (isinstance(monthly, int) and monthly > 0):
            errors.append(f"quota.monthly_limit: must be a positive token count in token mode (got '{monthly}')")

    metering = config.get("metering", {})
    if not isinstance(metering.get("enabled"), bool):
        errors.append(f"metering.enabled: must be true or false (got '{metering.get('enabled')}')")
    if metering.get("mode") not in ("shadow", "max"):
        errors.append(f"metering.mode: must be 'shadow' or 'max' (got '{metering.get('mode')}')")
    if metering.get("enabled") and not quota.get("enabled"):
        errors.append("metering.enabled: server-side metering requires quota.enabled=true")

    if config.get("settings_target") not in ("user", "managed"):
        errors.append(f"settings_target: must be 'user' or 'managed' (got '{config.get('settings_target')}')")

    web_search = config.get("web_search", {})
    entitled_groups = web_search.get("entitled_groups")
    if entitled_groups is not None and (
        not isinstance(entitled_groups, list) or not all(isinstance(g, str) and g.strip() for g in entitled_groups)
    ):
        errors.append(f"web_search.entitled_groups: must be a list of non-empty group names (got '{entitled_groups}')")
    if web_search.get("policy_mode") not in ("LOG_ONLY", "ENFORCE"):
        errors.append(
            f"web_search.policy_mode: must be 'LOG_ONLY' or 'ENFORCE' (got '{web_search.get('policy_mode')}')"
        )

    memory = config.get("memory", {})
    if memory.get("mode") not in ("extracted-only", "full"):
        errors.append(f"memory.mode: must be 'extracted-only' or 'full' (got '{memory.get('mode')}')")
    retention = memory.get("raw_event_retention_days")
    if not (isinstance(retention, int) and 3 <= retention <= 365):
        errors.append(f"memory.raw_event_retention_days: must be a number between 3 and 365 (got '{retention}')")
    org_write_groups = memory.get("org_write_groups")
    if org_write_groups is not None and (
        not isinstance(org_write_groups, list) or not all(isinstance(g, str) and g.strip() for g in org_write_groups)
    ):
        errors.append(f"memory.org_write_groups: must be a list of non-empty group names (got '{org_write_groups}')")
    skills = config.get("skills", {})
    for group_key in ("publisher_groups", "curator_groups"):
        groups = skills.get(group_key)
        if groups is not None and (
            not isinstance(groups, list) or not all(isinstance(g, str) and g.strip() for g in groups)
        ):
            errors.append(f"skills.{group_key}: must be a list of non-empty group names (got '{groups}')")
    org_id = skills.get("organization_id")
    if org_id and not re.match(r"^o-[a-z0-9]{10,32}$", str(org_id)):
        errors.append(f"skills.organization_id: must match o-xxxxxxxxxx (got '{org_id}')")
    if not isinstance(config.get("model_lifecycle", {}).get("enabled"), bool):
        errors.append(
            f"model_lifecycle.enabled: must be true or false (got '{config.get('model_lifecycle', {}).get('enabled')}')"
        )
    guardrails = config.get("guardrails", {})
    if not isinstance(guardrails.get("enabled"), bool):
        errors.append(f"guardrails.enabled: must be true or false (got '{guardrails.get('enabled')}')")
    if guardrails.get("content_filter_strength") not in ("LOW", "MEDIUM", "HIGH"):
        errors.append(
            "guardrails.content_filter_strength: must be LOW, MEDIUM, or HIGH "
            f"(got '{guardrails.get('content_filter_strength')}')"
        )
    include_list = guardrails.get("model_include_list")
    if include_list is not None and (
        not isinstance(include_list, list)
        or not all(isinstance(m, str) and m.strip() and "," not in m for m in include_list)
    ):
        errors.append(
            "guardrails.model_include_list: must be a list of non-empty model IDs without commas "
            f"(got '{include_list}')"
        )
    guardrail_name = guardrails.get("name")
    if guardrail_name and not re.match(r"^[0-9A-Za-z-_]{1,50}$", str(guardrail_name)):
        errors.append(f"guardrails.name: must match ^[0-9A-Za-z-_]{{1,50}}$ (got '{guardrail_name}')")

    if config.get("cowork", {}).get("config_delivery") != "static":
        errors.append(
            "cowork.config_delivery: GIP supports only 'static'; use the pinned AWS Samples "
            "claude-apps-gateway-bootstrap CDK for gateway-based Claude Desktop delivery "
            f"(got '{config.get('cowork', {}).get('config_delivery')}')"
        )

    distribution = config.get("distribution", {})
    dist_type = distribution.get("type")
    if dist_type not in (None, "presigned-s3", "landing-page"):
        errors.append(f"distribution.type: must be 'presigned-s3' or 'landing-page' (got '{dist_type}')")
    if dist_type == "landing-page":
        for field in ("idp_provider", "idp_domain", "idp_client_id", "idp_client_secret_arn", "custom_domain"):
            if not distribution.get(field):
                errors.append(
                    f"distribution.{field}: required for landing-page distribution "
                    "(secrets must be pre-created in Secrets Manager and referenced by ARN)"
                )
        if distribution.get("idp_provider") == "generic":
            for field in ("idp_issuer", "idp_authorization_endpoint", "idp_token_endpoint", "idp_userinfo_endpoint"):
                if not distribution.get(field):
                    errors.append(f"distribution.{field}: required for generic OIDC landing-page distribution")

    extra_files = config.get("extra_files")
    if extra_files:
        from governed_inference_platform.extra_files import validate_extra_files

        errors.extend(validate_extra_files(extra_files))

    extra_models = config.get("extra_models")
    if extra_models:
        from governed_inference_platform.models import validate_extra_models

        errors.extend(validate_extra_models(extra_models))

    return errors


def build_config_from_answers(answers: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Merge answers over DEFAULTS, derive wizard-equivalent values, validate.

    Returns:
        (merged config dict, list of error strings). The config is only usable
        when the error list is empty.
    """
    structural_errors = check_structure(answers)
    if structural_errors:
        return {}, structural_errors

    config = deep_merge(DEFAULTS, answers)
    errors: list[str] = []
    _apply_derivations(config, errors)
    errors.extend(validate_config(config))
    return config, errors


def _prune_none(node: Any) -> Any:
    """Recursively drop None values so exported answers stay minimal."""
    if isinstance(node, dict):
        return {k: _prune_none(v) for k, v in node.items() if v is not None}
    if isinstance(node, list):
        return [_prune_none(v) for v in node]
    return node


def build_answers_from_profile(command, profile_name: str) -> dict[str, Any]:
    """Rebuild an answers dict from a saved profile (export round-trip).

    Reuses ``InitCommand._check_existing_deployment`` (the wizard's own
    profile -> config rebuild) and augments it with the fields that rebuild
    does not carry (auth_type, IDC fields, redirect_port, settings_target,
    model_alias, CoWork chat flags) so export -> import is lossless.
    """
    answers = command._check_existing_deployment(profile_name)
    if not answers:
        raise ValueError(f"Profile not found or could not be read: {profile_name}")

    profile = Config.load().get_profile(profile_name)
    if profile is None:
        raise ValueError(f"Profile not found or could not be read: {profile_name}")
    answers.pop("_stacks_found", None)

    answers["auth_type"] = profile.effective_auth_type
    if profile.effective_auth_type != "oidc":
        # provider_domain/client_id are stored as "none" placeholders — drop them
        answers.pop("okta", None)
        answers.pop("credential_storage", None)
    if profile.effective_auth_type == "idc":
        for field in ("idc_start_url", "idc_account_id", "idc_permission_set_name", "sso_region"):
            value = getattr(profile, field, None)
            if value:
                answers[field] = value
    if profile.redirect_port:
        answers["redirect_port"] = profile.redirect_port
    answers["settings_target"] = profile.settings_target
    if profile.model_alias:
        answers["aws"]["model_alias"] = profile.model_alias
    # _check_existing_deployment only includes the chat flags when truthy —
    # set them explicitly so a disabled flag survives the round-trip
    answers.setdefault("cowork_3p", {})
    answers["cowork_3p"]["chat_tab_enabled"] = profile.cowork_chat_tab_enabled
    answers["cowork_3p"]["chat_advanced_file_analysis"] = profile.cowork_chat_advanced_file_analysis
    answers.setdefault("cowork", {})["config_delivery"] = "static"
    answers["metering"] = {
        "enabled": profile.metering_enabled,
        "mode": profile.metering_mode,
    }

    return _prune_none(answers)


def _print_errors(console: Console, header: str, errors: list[str]) -> None:
    console.print(f"[red]{header}[/red]")
    for error in errors:
        console.print(f"  - {error}", soft_wrap=True, markup=False, highlight=False)


def _print_summary(console: Console, config: dict[str, Any], profile_name: str) -> None:
    """Print a summary of the resulting profile."""
    console.print(f"\n[green]✓ Profile '{profile_name}' saved successfully (non-interactive).[/green]\n")
    auth_type = config.get("auth_type")
    if auth_type == "oidc":
        console.print(f"  Authentication:   OIDC ({config.get('provider_type')}: {config['okta']['domain']})")
        console.print(f"  Client ID:        {config['okta']['client_id']}")
        console.print(f"  Federation:       {config.get('federation_type')}")
    elif auth_type == "idc":
        console.print(f"  Authentication:   IAM Identity Center ({config.get('idc_start_url')})")
    else:
        console.print("  Authentication:   None (existing AWS credentials)")
    console.print(f"  AWS Region:       {config['aws']['region']}")
    console.print(f"  Stack base name:  {config['aws']['identity_pool_name']}")
    console.print(f"  Model:            {config['aws'].get('selected_model')}")
    console.print(f"  Bedrock regions:  {', '.join(config['aws'].get('allowed_bedrock_regions') or [])}")
    monitoring = config["monitoring"]
    console.print(
        f"  Monitoring:       {'enabled (' + str(monitoring.get('mode')) + ')' if monitoring.get('enabled') else 'disabled'}"
    )
    quota = config["quota"]
    if quota.get("enabled"):
        if quota.get("limit_type") == "cost":
            limits = f"${quota.get('monthly_cost_limit')}/user/month"
        else:
            limits = f"{quota.get('monthly_limit'):,} tokens/user/month"
        console.print(f"  Quota:            enabled ({quota.get('limit_type')}: {limits})")
    else:
        console.print("  Quota:            disabled")
    guardrails = config.get("guardrails", {})
    console.print(
        "  Guardrails:       "
        + (f"enabled ({guardrails.get('content_filter_strength')})" if guardrails.get("enabled") else "disabled")
    )
    dist = config["distribution"]
    console.print(f"  Distribution:     {dist.get('type') or 'disabled'}")
    console.print(f"  Settings target:  {config.get('settings_target')}")

    # Data-residency advisory (non-blocking) — same check as the wizard's review step
    from governed_inference_platform.cli.utils.residency import residency_warnings

    codebuild = config.get("codebuild", {})
    codebuild_region = (codebuild.get("region") or config["aws"]["region"]) if codebuild.get("enabled") else None
    warnings = residency_warnings(
        infra_region=config["aws"]["region"],
        cris_geography=config["aws"].get("cross_region_profile") or "us",
        web_search_enabled=config.get("web_search", {}).get("enabled", False),
        codebuild_region=codebuild_region,
        memory_enabled=config.get("memory", {}).get("enabled", False),
    )
    if warnings:
        console.print("\n[yellow]\\[Data residency][/yellow]")
        for warning in warnings:
            console.print(f"  ⚠ {warning}", soft_wrap=True, markup=False, highlight=False)
        console.print("  [dim]Advisory only — deployment is not blocked.[/dim]")

    console.print(f"\n  Saved to: {Config.PROFILES_DIR / (profile_name + '.json')}")
    console.print("\nNext steps:")
    console.print("  • Deploy infrastructure: [cyan]poetry run gip deploy[/cyan]")
    console.print("  • Create package:        [cyan]poetry run gip package[/cyan]")


def run_from_file(command, answers_path: str, profile_name: str | None, force: bool, console: Console) -> int:
    """Handle `gip init --from-file <answers.(yaml|json)>` (fully non-interactive)."""
    profile_name = profile_name or "default"
    if not Config._is_valid_profile_name(profile_name):
        console.print(
            f"[red]Invalid profile name '{profile_name}': must be alphanumeric with hyphens only, "
            "max 64 characters.[/red]"
        )
        return 1

    try:
        answers = load_answers_file(Path(answers_path))
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        return 1

    config, errors = build_config_from_answers(answers)
    if errors:
        _print_errors(console, f"Answers file is invalid ({len(errors)} problem(s)):", errors)
        return 1

    existing = Config.load().get_profile(profile_name)
    if existing and not force:
        console.print(
            f"[red]Profile '{profile_name}' already exists. Use --force to overwrite it.[/red]",
            soft_wrap=True,
        )
        return 1

    command._save_configuration(config, profile_name)
    _print_summary(console, config, profile_name)
    return 0


def run_export_answers(command, output_path: str, profile_name: str | None, console: Console) -> int:
    """Handle `gip init --export-answers <path>` (profile -> answers file)."""
    config = Config.load()
    name = profile_name or config.active_profile
    if not name:
        console.print("[red]No profile specified and no active profile set. Use --profile-name <name>.[/red]")
        return 1
    if not config.get_profile(name):
        console.print(f"[red]Profile not found: {name}[/red]")
        return 1

    try:
        answers = build_answers_from_profile(command, name)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        return 1

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".json":
        path.write_text(json.dumps(answers, indent=2) + "\n", encoding="utf-8")
    else:
        path.write_text(yaml.safe_dump(answers, default_flow_style=False, sort_keys=False), encoding="utf-8")

    console.print(f"[green]✓ Exported profile '{name}' to {path}[/green]")
    console.print(f"  Re-create it with: [cyan]gip init --from-file {path} --profile-name {name} --force[/cyan]")
    return 0
