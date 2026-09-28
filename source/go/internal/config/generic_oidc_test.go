package config

import (
	"testing"
)

// TestGenericOIDCConfig verifies that Generic OIDC fields (used by CyberArk,
// PingFederate, Keycloak, ForgeRock, etc.) parse correctly from config.json.
func TestGenericOIDCConfig(t *testing.T) {
	cfg, err := LoadProfileFromPath("testdata/generic_oidc_cyberark.json", "gip")
	if err != nil {
		t.Fatalf("load failed: %v", err)
	}
	if cfg.ProviderType != "generic" {
		t.Errorf("ProviderType = %q, want \"generic\"", cfg.ProviderType)
	}
	if cfg.OIDCIssuerURL != "https://abc1234.id.cyberark.cloud" {
		t.Errorf("OIDCIssuerURL = %q", cfg.OIDCIssuerURL)
	}
	if cfg.OIDCAuthorizationEndpoint != "https://abc1234.id.cyberark.cloud/OAuth2/Authorize/myapp" {
		t.Errorf("OIDCAuthorizationEndpoint = %q", cfg.OIDCAuthorizationEndpoint)
	}
	if cfg.OIDCTokenEndpoint != "https://abc1234.id.cyberark.cloud/OAuth2/Token/myapp" {
		t.Errorf("OIDCTokenEndpoint = %q", cfg.OIDCTokenEndpoint)
	}
	if cfg.OIDCJwksURI != "https://abc1234.id.cyberark.cloud/OAuth2/Keys/myapp" {
		t.Errorf("OIDCJwksURI = %q", cfg.OIDCJwksURI)
	}
	if cfg.OIDCThumbprint != "9e99a48a9960b14926bb7f3b02e22da2b0ab7280" {
		t.Errorf("OIDCThumbprint = %q", cfg.OIDCThumbprint)
	}
	if cfg.RedirectPort != 8401 {
		t.Errorf("RedirectPort = %d, want 8401", cfg.RedirectPort)
	}
	if cfg.CredentialStorage != "keyring" {
		t.Errorf("CredentialStorage = %q, want \"keyring\"", cfg.CredentialStorage)
	}
	if cfg.FederationType != "direct" {
		t.Errorf("FederationType = %q, want \"direct\"", cfg.FederationType)
	}
}
