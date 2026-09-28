# ABOUTME: Tests for the OIDC discovery helper — well-known endpoint fetch
# ABOUTME: All network calls are mocked; failure modes must surface as OidcDiscoveryError

from unittest.mock import MagicMock, patch

import pytest
import requests

from governed_inference_platform.cli.utils.oidc_discovery import (
    OidcDiscoveryError,
    discover_oidc_endpoints,
)

# --- discover_oidc_endpoints --------------------------------------------------


class TestDiscoverEndpoints:
    def _mock_response(self, status_code=200, json_data=None, raises_value_error=False):
        response = MagicMock()
        response.status_code = status_code
        if raises_value_error:
            response.json.side_effect = ValueError("not json")
        else:
            response.json.return_value = json_data
        return response

    @patch("governed_inference_platform.cli.utils.oidc_discovery.requests.get")
    def test_happy_path(self, mock_get):
        mock_get.return_value = self._mock_response(
            json_data={
                "issuer": "https://auth.example.com",
                "authorization_endpoint": "https://auth.example.com/as/authorization.oauth2",
                "token_endpoint": "https://auth.example.com/as/token.oauth2",
                "jwks_uri": "https://auth.example.com/pf/JWKS",
                "scopes_supported": ["openid", "profile"],  # extra fields ignored
            }
        )

        result = discover_oidc_endpoints("https://auth.example.com")

        assert result == {
            "issuer": "https://auth.example.com",
            "authorization_endpoint": "https://auth.example.com/as/authorization.oauth2",
            "token_endpoint": "https://auth.example.com/as/token.oauth2",
            "jwks_uri": "https://auth.example.com/pf/JWKS",
        }
        mock_get.assert_called_once_with("https://auth.example.com/.well-known/openid-configuration", timeout=10.0)

    @patch("governed_inference_platform.cli.utils.oidc_discovery.requests.get")
    def test_strips_trailing_slash_from_issuer(self, mock_get):
        mock_get.return_value = self._mock_response(
            json_data={
                "issuer": "https://auth.example.com",
                "authorization_endpoint": "https://auth.example.com/auth",
                "token_endpoint": "https://auth.example.com/token",
                "jwks_uri": "https://auth.example.com/jwks",
            }
        )

        discover_oidc_endpoints("https://auth.example.com/")

        # Trailing slash stripped before /.well-known is appended
        mock_get.assert_called_once_with("https://auth.example.com/.well-known/openid-configuration", timeout=10.0)

    def test_rejects_http_scheme(self):
        with pytest.raises(OidcDiscoveryError, match="must start with https"):
            discover_oidc_endpoints("http://auth.example.com")

    def test_rejects_no_scheme(self):
        with pytest.raises(OidcDiscoveryError, match="must start with https"):
            discover_oidc_endpoints("auth.example.com")

    @patch("governed_inference_platform.cli.utils.oidc_discovery.requests.get")
    def test_network_error(self, mock_get):
        mock_get.side_effect = requests.ConnectionError("DNS lookup failed")
        with pytest.raises(OidcDiscoveryError, match="Could not reach"):
            discover_oidc_endpoints("https://auth.example.com")

    @patch("governed_inference_platform.cli.utils.oidc_discovery.requests.get")
    def test_timeout(self, mock_get):
        mock_get.side_effect = requests.Timeout("timed out")
        with pytest.raises(OidcDiscoveryError, match="Could not reach"):
            discover_oidc_endpoints("https://auth.example.com")

    @patch("governed_inference_platform.cli.utils.oidc_discovery.requests.get")
    def test_404(self, mock_get):
        mock_get.return_value = self._mock_response(status_code=404)
        with pytest.raises(OidcDiscoveryError, match="HTTP 404"):
            discover_oidc_endpoints("https://auth.example.com")

    @patch("governed_inference_platform.cli.utils.oidc_discovery.requests.get")
    def test_500(self, mock_get):
        mock_get.return_value = self._mock_response(status_code=500)
        with pytest.raises(OidcDiscoveryError, match="HTTP 500"):
            discover_oidc_endpoints("https://auth.example.com")

    @patch("governed_inference_platform.cli.utils.oidc_discovery.requests.get")
    def test_non_json_response(self, mock_get):
        mock_get.return_value = self._mock_response(raises_value_error=True)
        with pytest.raises(OidcDiscoveryError, match="non-JSON"):
            discover_oidc_endpoints("https://auth.example.com")

    @patch("governed_inference_platform.cli.utils.oidc_discovery.requests.get")
    def test_json_array_rejected(self, mock_get):
        mock_get.return_value = self._mock_response(json_data=["not", "an", "object"])
        with pytest.raises(OidcDiscoveryError, match="not an object"):
            discover_oidc_endpoints("https://auth.example.com")

    @patch("governed_inference_platform.cli.utils.oidc_discovery.requests.get")
    def test_missing_required_field(self, mock_get):
        # Missing token_endpoint and jwks_uri
        mock_get.return_value = self._mock_response(
            json_data={
                "issuer": "https://auth.example.com",
                "authorization_endpoint": "https://auth.example.com/auth",
            }
        )
        with pytest.raises(OidcDiscoveryError, match="missing required fields"):
            discover_oidc_endpoints("https://auth.example.com")

    @patch("governed_inference_platform.cli.utils.oidc_discovery.requests.get")
    def test_empty_string_field_treated_as_missing(self, mock_get):
        mock_get.return_value = self._mock_response(
            json_data={
                "issuer": "https://auth.example.com",
                "authorization_endpoint": "https://auth.example.com/auth",
                "token_endpoint": "",  # falsy — should count as missing
                "jwks_uri": "https://auth.example.com/jwks",
            }
        )
        with pytest.raises(OidcDiscoveryError, match="token_endpoint"):
            discover_oidc_endpoints("https://auth.example.com")


def test_module_has_no_thumbprint_computation():
    """The JWKS thumbprint helper was removed: the IAM OIDC provider's ThumbprintList is
    optional (IAM retrieves the CA thumbprint itself), so no hash is computed client-side."""
    import governed_inference_platform.cli.utils.oidc_discovery as mod

    assert not hasattr(mod, "compute_jwks_thumbprint")
    assert not hasattr(mod, "hashlib")
