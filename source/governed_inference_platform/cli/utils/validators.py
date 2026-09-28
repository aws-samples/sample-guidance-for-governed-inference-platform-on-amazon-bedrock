# ABOUTME: Input validation functions for CLI commands
# ABOUTME: Validates user input for domains, regions, and other parameters

"""Input validators for CLI commands."""

import re


def validate_okta_domain(domain: str) -> bool:
    """Validate Okta domain format.

    Valid formats:
    - company.okta.com
    - company.oktapreview.com
    - company.okta-emea.com
    - dev-12345678.okta.com
    """
    if not domain:
        return False

    # Remove protocol if present
    domain = domain.replace("https://", "").replace("http://", "")

    # Check format
    pattern = r"^[a-zA-Z0-9][a-zA-Z0-9-]*\.okta(-emea)?\.com$|^[a-zA-Z0-9][a-zA-Z0-9-]*\.oktapreview\.com$"
    return bool(re.match(pattern, domain))


def validate_oidc_provider_domain(domain: str) -> bool:
    """Validate generic OIDC provider domain format.

    Valid formats:
    - company.okta.com
    - login.microsoftonline.com/{tenant-id}/v2.0
    - accounts.google.com
    - auth.example.com
    - cognito-idp.{region}.amazonaws.com/{user-pool-id}
    """
    if not domain:
        return False

    # Remove protocol if present
    domain = domain.replace("https://", "").replace("http://", "")

    # Basic validation: must have at least a domain name
    # Allow paths for providers like Microsoft that require them
    # Must start with alphanumeric, can contain dots, hyphens, slashes
    # Minimum: x.y format (at least one dot)
    pattern = r"^[a-zA-Z0-9][a-zA-Z0-9.-]+\.[a-zA-Z0-9]+(/[a-zA-Z0-9._~:/?#[\]@!$&\'()*+,;=-]*)?$"
    return bool(re.match(pattern, domain))


def validate_aws_region(region: str) -> bool:
    """Validate AWS region format."""
    if not region:
        return False

    # AWS region format: us-east-1, eu-west-2, etc.
    pattern = r"^[a-z]{2}-[a-z]+-\d{1,2}$"
    return bool(re.match(pattern, region))


def validate_bedrock_regions(regions: list[str]) -> bool:
    """Validate list of AWS regions for Bedrock."""
    if not regions:
        return False

    return all(validate_aws_region(region.strip()) for region in regions)


def validate_stack_name(name: str) -> bool:
    """Validate CloudFormation stack name."""
    if not name or len(name) > 128:
        return False

    # Stack names can contain only alphanumeric characters and hyphens
    pattern = r"^[a-zA-Z][a-zA-Z0-9-]*$"
    return bool(re.match(pattern, name))


def validate_client_id(client_id: str) -> bool:
    """Validate OIDC client ID format."""
    if not client_id or len(client_id) < 10:
        return False

    # Client IDs can be alphanumeric with hyphens (for Microsoft/Azure)
    # Examples:
    # - Okta: 0oa1234567890abcde
    # - Microsoft: 12345678-1234-1234-1234-123456789012
    # - Google: 123456789012-abcdefghijklmnopqrstuvwxyz1234.apps.googleusercontent.com
    pattern = r"^[a-zA-Z0-9][a-zA-Z0-9.\-_]+$"
    return bool(re.match(pattern, client_id))
