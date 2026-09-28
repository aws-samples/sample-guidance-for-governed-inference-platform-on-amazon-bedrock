# ABOUTME: OIDC discovery helper — fetches the .well-known/openid-configuration document
# ABOUTME: Used by `gip init` to auto-populate generic OIDC profile fields

"""OIDC discovery.

discover_oidc_endpoints raises OidcDiscoveryError on any failure. Callers should
catch and fall back to manual entry — this helper is convenience, not
correctness-critical.

No JWKS TLS thumbprint is computed here: the IAM OIDC provider's ThumbprintList
is optional (IAM retrieves the CA thumbprint itself when it is omitted), so the
generic-OIDC flow only accepts an operator-supplied override for private-CA hosts.
"""

from __future__ import annotations

from typing import Any

import requests


class OidcDiscoveryError(Exception):
    """Raised when OIDC discovery fails."""


REQUIRED_DISCOVERY_FIELDS = ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri")


def discover_oidc_endpoints(issuer_url: str, timeout: float = 10.0) -> dict[str, str]:
    """Fetch {issuer}/.well-known/openid-configuration and return endpoint URLs.

    Args:
        issuer_url: Issuer URL, e.g. https://auth.example.com (with or without trailing slash).
        timeout: HTTP timeout in seconds.

    Returns:
        Dict with keys: issuer, authorization_endpoint, token_endpoint, jwks_uri.

    Raises:
        OidcDiscoveryError: On network error, non-200 response, non-JSON body, or
            missing required fields. The message is suitable for user display.
    """
    if not issuer_url.startswith("https://"):
        raise OidcDiscoveryError(f"Issuer URL must start with https:// — got {issuer_url!r}")

    discovery_url = issuer_url.rstrip("/") + "/.well-known/openid-configuration"

    try:
        response = requests.get(discovery_url, timeout=timeout)
    except requests.RequestException as e:
        raise OidcDiscoveryError(f"Could not reach {discovery_url}: {e}") from e

    if response.status_code != 200:
        raise OidcDiscoveryError(f"Discovery endpoint returned HTTP {response.status_code} from {discovery_url}")

    try:
        data: Any = response.json()
    except ValueError as e:
        raise OidcDiscoveryError(f"Discovery endpoint returned non-JSON response: {e}") from e

    if not isinstance(data, dict):
        raise OidcDiscoveryError("Discovery endpoint returned JSON that is not an object")

    missing = [f for f in REQUIRED_DISCOVERY_FIELDS if not data.get(f)]
    if missing:
        raise OidcDiscoveryError(f"Discovery response missing required fields: {', '.join(missing)}")

    return {f: data[f] for f in REQUIRED_DISCOVERY_FIELDS}
