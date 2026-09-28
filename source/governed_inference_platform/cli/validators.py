# ABOUTME: Configuration validators run at package time to catch misconfigurations early.
# ABOUTME: Called by the package command before generating any distribution files.

"""Configuration validators run at package time to catch misconfigurations early."""

from dataclasses import dataclass


@dataclass
class ValidationError:
    field: str
    message: str
    severity: str = "error"  # "error" or "warning"


def validate_profile_for_packaging(profile) -> list[ValidationError]:
    """Validate a profile is consistent and ready for packaging."""
    errors = []

    auth_type = getattr(profile, "effective_auth_type", getattr(profile, "auth_type", "oidc"))

    # IDC requires start URL
    if auth_type == "idc":
        if not getattr(profile, "idc_start_url", None):
            errors.append(ValidationError("idc_start_url", "IDC auth requires idc_start_url"))
        if not getattr(profile, "idc_account_id", None):
            errors.append(ValidationError("idc_account_id", "IDC auth requires idc_account_id"))
        if not getattr(profile, "idc_permission_set_name", None):
            errors.append(ValidationError("idc_permission_set_name", "IDC auth requires idc_permission_set_name"))

    # OIDC requires provider domain + client ID
    if auth_type == "oidc":
        if not getattr(profile, "provider_domain", None) and not getattr(profile, "oidc_issuer_url", None):
            errors.append(ValidationError("provider_domain", "OIDC auth requires provider_domain or oidc_issuer_url"))
        if not getattr(profile, "client_id", None):
            errors.append(ValidationError("client_id", "OIDC auth requires client_id"))

    # Region validation
    if not getattr(profile, "aws_region", None):
        errors.append(ValidationError("aws_region", "AWS region is required"))

    allowed_regions = getattr(profile, "allowed_bedrock_regions", None)
    if allowed_regions and getattr(profile, "aws_region", None):
        if profile.aws_region not in allowed_regions:
            errors.append(
                ValidationError(
                    "aws_region",
                    f"Region '{profile.aws_region}' not in allowed_bedrock_regions: {allowed_regions}",
                    severity="warning",
                )
            )

    # Monitoring consistency
    if getattr(profile, "monitoring_enabled", False):
        endpoint = getattr(profile, "otel_collector_endpoint", None)
        if not endpoint:
            errors.append(
                ValidationError(
                    "otel_collector_endpoint",
                    "Monitoring enabled but no otel_collector_endpoint configured. Run 'gip deploy monitoring' first.",
                    severity="warning",
                )
            )

    config_delivery = getattr(profile, "cowork_config_delivery", "static")
    if config_delivery != "static":
        errors.append(
            ValidationError(
                "cowork_config_delivery",
                "Legacy GIP bootstrap delivery is retired. Use static GIP packaging or the pinned AWS Samples "
                "claude-apps-gateway-bootstrap CDK.",
                severity="warning",
            )
        )

    # Quota enforcement requires quota API endpoint
    quota_api_endpoint = getattr(profile, "quota_api_endpoint", None)
    quota_enabled = getattr(profile, "quota_monitoring_enabled", False) or bool(quota_api_endpoint)
    quota_blocks = quota_enabled and "block" in (
        getattr(profile, "daily_enforcement_mode", "alert"),
        getattr(profile, "monthly_enforcement_mode", "alert"),
    )
    legacy_enforcement = getattr(profile, "quota_enforcement_mode", "off") != "off"
    if quota_blocks or legacy_enforcement:
        if not quota_api_endpoint:
            errors.append(
                ValidationError(
                    "quota_api_endpoint",
                    "Blocking quota enforcement requires quota_api_endpoint",
                )
            )

    return errors
