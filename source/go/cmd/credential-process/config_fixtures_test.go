package main

import (
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"testing"

	"gip-go/internal/config"
)

// fixtureDir returns the absolute path to the testdata/configs directory
// relative to this test file.
func fixtureDir(t *testing.T) string {
	t.Helper()
	_, thisFile, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("runtime.Caller failed")
	}
	return filepath.Join(filepath.Dir(thisFile), "testdata", "configs")
}

// getField returns the string value of a named field on ProfileConfig using
// reflection. Fatals the test if the field doesn't exist or isn't a string.
func getField(t *testing.T, p *config.ProfileConfig, field string) string {
	t.Helper()
	v := reflect.ValueOf(p).Elem().FieldByName(field)
	if !v.IsValid() {
		t.Fatalf("ProfileConfig has no field %q", field)
	}
	if v.Kind() != reflect.String {
		t.Fatalf("field %q is %s, not string", field, v.Kind())
	}
	return v.String()
}

// TestConfigFixtures_ParseAllAuthTypes verifies that the config loader
// correctly parses real-world config.json fixtures for every supported
// auth type. This catches regressions like missing fields or changed
// JSON tags that would break specific auth flows.
func TestConfigFixtures_ParseAllAuthTypes(t *testing.T) {
	dir := fixtureDir(t)

	tests := []struct {
		name        string
		file        string
		profileName string
		wantFields  map[string]string // field name → expected value
	}{
		{
			name:        "Okta OIDC with direct federation",
			file:        "oidc_okta.json",
			profileName: "gip",
			wantFields: map[string]string{
				"ProviderDomain":   "dev-12345678.okta.com",
				"ClientID":         "0oa0000000000000000",
				"ProviderType":     "okta",
				"AWSRegion":        "us-east-1",
				"FederatedRoleARN": "arn:aws:iam::123456789012:role/OktaFederatedRole",
				"FederationType":   "direct",
				"OktaAuthServerID": "default",
			},
		},
		{
			name:        "Google OIDC with client_secret",
			file:        "oidc_google.json",
			profileName: "gip",
			wantFields: map[string]string{
				"ProviderDomain":   "accounts.google.com",
				"ClientID":         "123456789012-abcdefghijklmnop.apps.googleusercontent.com",
				"ClientSecret":     "GOCSPX-0000000000000000",
				"ProviderType":     "google",
				"AWSRegion":        "us-west-2",
				"FederatedRoleARN": "arn:aws:iam::111122223333:role/GoogleFederatedRole",
				"OIDCIssuerURL":    "https://accounts.google.com",
			},
		},
		{
			name:        "Azure AD with Cognito federation",
			file:        "oidc_azure.json",
			profileName: "gip",
			wantFields: map[string]string{
				"ProviderDomain": "login.microsoftonline.com/00000000-0000-0000-0000-000000000000/v2.0",
				"ClientID":       "00000000-0000-0000-0000-000000000000",
				"ProviderType":   "azure",
				"AWSRegion":      "eu-west-1",
				"IdentityPoolID": "eu-west-1:00000000-0000-0000-0000-000000000000",
				"RoleARN":        "arn:aws:iam::444455556666:role/AzureCognitoRole",
				"FederationType": "cognito",
				"AzureAuthMode":  "public",
			},
		},
		{
			name:        "Generic OIDC with custom endpoints",
			file:        "oidc_generic.json",
			profileName: "gip",
			wantFields: map[string]string{
				"ProviderDomain":            "idp.example.corp",
				"ClientID":                  "gip-credential-process",
				"ProviderType":              "generic",
				"AWSRegion":                 "ap-southeast-2",
				"FederatedRoleARN":          "arn:aws:iam::777788889999:role/GenericOIDCRole",
				"OIDCIssuerURL":             "https://idp.example.corp/realms/main",
				"OIDCAuthorizationEndpoint": "https://idp.example.corp/realms/main/protocol/openid-connect/auth",
				"OIDCTokenEndpoint":         "https://idp.example.corp/realms/main/protocol/openid-connect/token",
				"OIDCJwksURI":               "https://idp.example.corp/realms/main/protocol/openid-connect/certs",
				"OIDCThumbprint":            "9e99a48a9960b14926bb7f3b02e22da2b0ab7280",
			},
		},
		{
			name:        "IAM Identity Center",
			file:        "idc.json",
			profileName: "gip",
			wantFields: map[string]string{
				"AuthType":             "idc",
				"AWSRegion":            "us-east-1",
				"IDCStartURL":          "https://d-9067abcdef.awsapps.com/start",
				"IDCAccountID":         "123456789012",
				"IDCPermissionSetName": "GIPDeveloper",
				"IDCRegion":            "us-east-1",
			},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			path := filepath.Join(dir, tt.file)
			profile, err := config.LoadProfileFromPath(path, tt.profileName)
			if err != nil {
				t.Fatalf("LoadProfileFromPath(%s, %s): %v", tt.file, tt.profileName, err)
			}

			for field, want := range tt.wantFields {
				got := getField(t, profile, field)
				if got != want {
					t.Errorf("field %s = %q, want %q", field, got, want)
				}
			}
		})
	}
}

// TestConfigFixtures_IDCFields verifies all IAM Identity Center-specific fields
// parse correctly and that the IsIDC() method returns true.
func TestConfigFixtures_IDCFields(t *testing.T) {
	dir := fixtureDir(t)
	path := filepath.Join(dir, "idc.json")

	profile, err := config.LoadProfileFromPath(path, "gip")
	if err != nil {
		t.Fatalf("LoadProfileFromPath: %v", err)
	}

	// Verify auth_type
	if profile.AuthType != "idc" {
		t.Errorf("AuthType = %q, want %q", profile.AuthType, "idc")
	}

	// Verify IsIDC() helper
	if !profile.IsIDC() {
		t.Error("IsIDC() = false, want true for IDC profile")
	}

	// Verify all IDC-specific fields
	if profile.IDCStartURL == "" {
		t.Error("IDCStartURL is empty")
	}
	if !strings.HasPrefix(profile.IDCStartURL, "https://") {
		t.Errorf("IDCStartURL = %q, want https:// prefix", profile.IDCStartURL)
	}

	if profile.IDCAccountID == "" {
		t.Error("IDCAccountID is empty")
	}
	if len(profile.IDCAccountID) != 12 {
		t.Errorf("IDCAccountID = %q, want 12-digit AWS account ID", profile.IDCAccountID)
	}

	if profile.IDCPermissionSetName == "" {
		t.Error("IDCPermissionSetName is empty")
	}

	if profile.IDCRegion == "" {
		t.Error("IDCRegion is empty")
	}

	// Verify cost attribution fields
	if profile.CostAttributionTagKey != "CostCenter" {
		t.Errorf("CostAttributionTagKey = %q, want %q", profile.CostAttributionTagKey, "CostCenter")
	}
}

// TestConfigFixtures_GenericOIDCEndpoints verifies that all custom endpoint
// URLs parse correctly for the generic OIDC provider type.
func TestConfigFixtures_GenericOIDCEndpoints(t *testing.T) {
	dir := fixtureDir(t)
	path := filepath.Join(dir, "oidc_generic.json")

	profile, err := config.LoadProfileFromPath(path, "gip")
	if err != nil {
		t.Fatalf("LoadProfileFromPath: %v", err)
	}

	if profile.ProviderType != "generic" {
		t.Errorf("ProviderType = %q, want %q", profile.ProviderType, "generic")
	}

	// All generic OIDC endpoint fields must be populated
	endpoints := map[string]string{
		"OIDCIssuerURL":             profile.OIDCIssuerURL,
		"OIDCAuthorizationEndpoint": profile.OIDCAuthorizationEndpoint,
		"OIDCTokenEndpoint":         profile.OIDCTokenEndpoint,
		"OIDCJwksURI":               profile.OIDCJwksURI,
	}

	for name, value := range endpoints {
		if value == "" {
			t.Errorf("%s is empty", name)
			continue
		}
		if !strings.HasPrefix(value, "https://") {
			t.Errorf("%s = %q, want https:// prefix", name, value)
		}
	}

	// Thumbprint should be a 40-char hex IAM thumbprint (optional in new
	// profiles; this fixture keeps one to prove older configs still load)
	if len(profile.OIDCThumbprint) != 40 {
		t.Errorf("OIDCThumbprint length = %d, want 40 (40-char hex IAM thumbprint)", len(profile.OIDCThumbprint))
	}

	// RedirectPort should be overridden from default
	if profile.RedirectPort != 9876 {
		t.Errorf("RedirectPort = %d, want 9876", profile.RedirectPort)
	}
}

// TestConfigFixtures_GoogleClientSecret verifies that the Google OIDC fixture
// has a non-confidential client_secret populated (required by Google's OAuth
// for installed/native apps) and the issuer URL matches Google's pattern.
func TestConfigFixtures_GoogleClientSecret(t *testing.T) {
	dir := fixtureDir(t)
	path := filepath.Join(dir, "oidc_google.json")

	profile, err := config.LoadProfileFromPath(path, "gip")
	if err != nil {
		t.Fatalf("LoadProfileFromPath: %v", err)
	}

	// Google requires client_secret even for desktop apps (non-confidential)
	if profile.ClientSecret == "" {
		t.Error("ClientSecret is empty; Google OIDC requires non-confidential secret")
	}
	if !strings.HasPrefix(profile.ClientSecret, "GOCSPX-") {
		t.Errorf("ClientSecret = %q, want GOCSPX- prefix (Google format)", profile.ClientSecret)
	}

	// Issuer URL should be Google's
	if profile.OIDCIssuerURL != "https://accounts.google.com" {
		t.Errorf("OIDCIssuerURL = %q, want %q", profile.OIDCIssuerURL, "https://accounts.google.com")
	}

	// ProviderDomain should match Google accounts
	if profile.ProviderDomain != "accounts.google.com" {
		t.Errorf("ProviderDomain = %q, want %q", profile.ProviderDomain, "accounts.google.com")
	}
}

// TestConfigFixtures_AzureCognito verifies the Azure AD fixture parses
// Cognito federation fields and Azure-specific auth mode correctly.
func TestConfigFixtures_AzureCognito(t *testing.T) {
	dir := fixtureDir(t)
	path := filepath.Join(dir, "oidc_azure.json")

	profile, err := config.LoadProfileFromPath(path, "gip")
	if err != nil {
		t.Fatalf("LoadProfileFromPath: %v", err)
	}

	// Azure should use Cognito federation
	if profile.FederationType != "cognito" {
		t.Errorf("FederationType = %q, want %q", profile.FederationType, "cognito")
	}
	if profile.IdentityPoolID == "" {
		t.Error("IdentityPoolID is empty for Cognito federation")
	}
	if profile.RoleARN == "" {
		t.Error("RoleARN is empty for Cognito federation")
	}

	// Azure auth mode
	if profile.AzureAuthMode != "public" {
		t.Errorf("AzureAuthMode = %q, want %q", profile.AzureAuthMode, "public")
	}

	// MaxSessionDuration for cognito should be 28800
	if profile.MaxSessionDuration != 28800 {
		t.Errorf("MaxSessionDuration = %d, want 28800", profile.MaxSessionDuration)
	}
}

// TestConfigFixtures_DefaultsApplied verifies that missing fields get
// appropriate defaults after loading.
func TestConfigFixtures_DefaultsApplied(t *testing.T) {
	dir := fixtureDir(t)

	tests := []struct {
		name        string
		file        string
		profileName string
	}{
		{"Okta", "oidc_okta.json", "gip"},
		{"Google", "oidc_google.json", "gip"},
		{"Azure", "oidc_azure.json", "gip"},
		{"Generic", "oidc_generic.json", "gip"},
		{"IDC", "idc.json", "gip"},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			path := filepath.Join(dir, tt.file)
			profile, err := config.LoadProfileFromPath(path, tt.profileName)
			if err != nil {
				t.Fatalf("LoadProfileFromPath(%s, %s): %v", tt.file, tt.profileName, err)
			}

			// Defaults that must always be set
			if profile.AWSRegion == "" {
				t.Error("AWSRegion is empty (should default to us-east-1)")
			}
			if profile.CredentialStorage == "" {
				t.Error("CredentialStorage is empty (should default to session)")
			}
			if profile.QuotaFailMode == "" {
				t.Error("QuotaFailMode is empty (should default to closed)")
			}
			if profile.QuotaCheckInterval == 0 {
				t.Error("QuotaCheckInterval is 0 (should default to 30)")
			}
			if profile.QuotaCheckTimeout == 0 {
				t.Error("QuotaCheckTimeout is 0 (should default to 5)")
			}
			if profile.FederationType == "" {
				t.Error("FederationType is empty (should be auto-detected)")
			}
			if profile.MaxSessionDuration == 0 {
				t.Error("MaxSessionDuration is 0 (should have default)")
			}
		})
	}
}

// TestConfigFixtures_ProfileNotFound verifies that loading a non-existent
// profile returns an appropriate error for both config formats.
func TestConfigFixtures_ProfileNotFound(t *testing.T) {
	dir := fixtureDir(t)

	tests := []struct {
		name string
		file string
	}{
		{"new format", "oidc_okta.json"},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			path := filepath.Join(dir, tt.file)
			_, err := config.LoadProfileFromPath(path, "NonExistentProfile")
			if err == nil {
				t.Fatal("expected error for non-existent profile, got nil")
			}
			if !strings.Contains(err.Error(), "not found") {
				t.Errorf("error = %q, want to contain %q", err.Error(), "not found")
			}
		})
	}
}
