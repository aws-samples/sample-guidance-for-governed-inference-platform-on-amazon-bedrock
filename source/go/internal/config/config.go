package config

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
)

// ProfileConfig represents a single profile's configuration from config.json.
type ProfileConfig struct {
	ProviderDomain    string `json:"provider_domain"`
	ClientID          string `json:"client_id"`
	ProviderType      string `json:"provider_type"`
	AWSRegion         string `json:"aws_region"`
	CredentialStorage string `json:"credential_storage"`

	// Federation - Direct STS
	FederatedRoleARN string `json:"federated_role_arn"`
	FederationType   string `json:"federation_type"`

	// Federation - Cognito
	IdentityPoolID    string `json:"identity_pool_id"`
	IdentityPoolName  string `json:"identity_pool_name"`
	CognitoUserPoolID string `json:"cognito_user_pool_id"`
	RoleARN           string `json:"role_arn"`

	// Session
	MaxSessionDuration int `json:"max_session_duration"`

	// Mirrors the auth stack's SessionNameBinding parameter ("none" | "email"
	// | "sub"). In "email"/"sub" mode the role trust policy requires the STS
	// RoleSessionName to equal that raw token claim, so buildSessionName uses
	// the claim verbatim and fails closed on invalid/missing values. Absent or
	// "" behaves as "none" (legacy naming) — older bundles keep working.
	SessionNameBinding string `json:"session_name_binding,omitempty"`

	// Quota
	QuotaAPIEndpoint       string `json:"quota_api_endpoint"`
	QuotaFailMode          string `json:"quota_fail_mode"`
	QuotaCheckInterval     int    `json:"quota_check_interval"`
	QuotaCheckTimeout      int    `json:"quota_check_timeout"`
	DailyEnforcementMode   string `json:"daily_enforcement_mode,omitempty"`   // "alert" | "block"
	MonthlyEnforcementMode string `json:"monthly_enforcement_mode,omitempty"` // "alert" | "block"
	EnableFineGrained      bool   `json:"enable_finegrained_quotas,omitempty"`

	// Okta Custom Authorization Server id. Absent / empty / "default" all
	// mean "use the default CAS" -- the Go code normalizes these equivalently.
	OktaAuthServerID string `json:"okta_auth_server_id"`
	// AWS session-tag key used for cost attribution. Default "Project" matches
	// the historical behavior; customers who standardize on a different name
	// (CostCenter, BillingCode, etc.) override here. Callers resolve absent or
	// empty to "Project" so older bundles that predate this field keep working.
	CostAttributionTagKey string `json:"cost_attribution_tag_key,omitempty"`

	// Generic OIDC provider (provider_type == "generic").
	// Full absolute URLs for IdPs that aren't Okta/Auth0/Azure/Cognito
	// (e.g. CyberArk, PingFederate, Keycloak, ForgeRock).
	OIDCIssuerURL             string `json:"oidc_issuer_url,omitempty"`
	OIDCAuthorizationEndpoint string `json:"oidc_authorization_endpoint,omitempty"`
	OIDCTokenEndpoint         string `json:"oidc_token_endpoint,omitempty"`
	OIDCJwksURI               string `json:"oidc_jwks_uri,omitempty"`
	OIDCThumbprint            string `json:"oidc_thumbprint,omitempty"`

	// Custom OAuth callback port (default 8400). REDIRECT_PORT env var takes precedence.
	RedirectPort int `json:"redirect_port,omitempty"`

	// OIDC prompt parameter sent during the authorization request.
	// Default "select_account" for Azure (shows account picker).
	// Set to "" to let the IdP reuse the existing browser session silently —
	// useful for single-tenant enterprise deployments with one account.
	OIDCPrompt *string `json:"oidc_prompt,omitempty"`

	// Azure AD confidential-client auth. "" or "public" -> PKCE-only public client.
	// "secret" -> look up client_secret from OS keyring at token-exchange time.
	// "certificate" -> sign a JWT client assertion with a PEM cert + private key.
	// Only meaningful when provider_type == "azure"; ignored otherwise.
	AzureAuthMode            string `json:"azure_auth_mode,omitempty"`
	ClientCertificatePath    string `json:"client_certificate_path,omitempty"`
	ClientCertificateKeyPath string `json:"client_certificate_key_path,omitempty"`

	// SSO authentication. When false, the credential helper bypasses the OIDC
	// flow entirely and emits AWS credentials from the ambient credential
	// chain (IAM Identity Center, env vars, instance profile, etc.). Mirrors
	// the Python credential_provider behavior added in PR #303.
	// Pointer so we can distinguish "missing" (legacy bundles, default true)
	// from "explicitly false" (passthrough mode).
	SsoEnabled *bool `json:"sso_enabled,omitempty"`

	// Non-confidential client secret read from config.json (e.g. Google Desktop
	// OAuth requires a client_secret for token exchange, but Google documents it
	// as non-confidential for installed/native apps). Empty means PKCE-only.
	ClientSecret string `json:"client_secret,omitempty"`

	// IAM Identity Center fields (populated when auth_type == "idc").
	// When present, credential-process drives the SSO OIDC device-auth flow
	// directly (auto-opens browser) instead of relying on ambient credentials.
	AuthType             string `json:"auth_type,omitempty"`               // "oidc" | "idc" | ""
	IDCStartURL          string `json:"idc_start_url,omitempty"`           // e.g. https://d-xxxxxxxxxx.awsapps.com/start
	IDCAccountID         string `json:"idc_account_id,omitempty"`          // AWS account ID for role assumption
	IDCPermissionSetName string `json:"idc_permission_set_name,omitempty"` // IAM role name / permission set
	IDCRegion            string `json:"idc_region,omitempty"`              // SSO endpoint region (defaults to aws_region)

	// Monitoring
	MonitoringEnabled     bool   `json:"monitoring_enabled,omitempty"`      // Whether telemetry collection is active
	MonitoringMode        string `json:"monitoring_mode,omitempty"`         // "central" (ECS Fargate) | "sidecar" (local collector)
	OtelCollectorEndpoint string `json:"otel_collector_endpoint,omitempty"` // ALB/collector URL for OTLP export

	// Bedrock Guardrails account-level enforcement. The Go credential helper does
	// not enforce these values, but it must preserve config parity with Python.
	GuardrailsEnabled               bool     `json:"guardrails_enabled,omitempty"`
	GuardrailsName                  string   `json:"guardrails_name,omitempty"`
	GuardrailsContentFilterStrength string   `json:"guardrails_content_filter_strength,omitempty"`
	GuardrailsModelIncludeList      []string `json:"guardrails_model_include_list,omitempty"`
	GuardrailsKMSKeyARN             string   `json:"guardrails_kms_key_arn,omitempty"`

	// Admin deployment-readiness fields. The credential helper does not consume
	// these values, but they remain in schema parity with the Python Profile.
	GatewayDeploymentMode             string `json:"gateway_deployment_mode,omitempty"`
	GatewayImageDigest                string `json:"gateway_image_digest,omitempty"`
	GatewayDBMultiAZ                  bool   `json:"gateway_db_multi_az,omitempty"`
	GatewayDBDeletionProtection       bool   `json:"gateway_db_deletion_protection,omitempty"`
	GatewayDesiredCount               int    `json:"gateway_desired_count,omitempty"`
	WebsearchDeploymentMode           string `json:"websearch_deployment_mode,omitempty"`
	WebsearchPolicyValidationComplete bool   `json:"websearch_policy_validation_complete,omitempty"`

	// Bootstrap server (dynamic config delivery for CoWork)
	BootstrapEndpoint  string `json:"bootstrap_endpoint,omitempty"`   // Lambda URL for per-user config at sign-in
	ConfigDeliveryMode string `json:"config_delivery_mode,omitempty"` // "static" | "bootstrap"
}

// configFile represents the on-disk config.json format.
type configFile struct {
	Profiles map[string]ProfileConfig `json:"profiles"`
}

// LoadProfile loads a named profile from config.json.
// Search order: directory of the running binary, then ~/gip/.
func LoadProfile(profileName string) (*ProfileConfig, error) {
	data, err := readConfigFile()
	if err != nil {
		return nil, err
	}
	return parseProfile(data, profileName)
}

// LoadProfileFromPath loads a named profile from an explicit config.json path.
// Exposed for regression tests that need to drive the loader against historical
// fixture files without touching the real search paths.
func LoadProfileFromPath(path, profileName string) (*ProfileConfig, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("reading %s: %w", path, err)
	}
	return parseProfile(data, profileName)
}

func parseProfile(data []byte, profileName string) (*ProfileConfig, error) {
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(data, &raw); err != nil {
		return nil, fmt.Errorf("invalid config.json: %w", err)
	}
	if _, ok := raw["profiles"]; !ok {
		return nil, fmt.Errorf("invalid config.json: missing \"profiles\" key")
	}

	var cf configFile
	if err := json.Unmarshal(data, &cf); err != nil {
		return nil, fmt.Errorf("invalid config.json: %w", err)
	}
	profile, ok := cf.Profiles[profileName]
	if !ok {
		return nil, fmt.Errorf("profile %q not found in configuration", profileName)
	}

	// Handle identity_pool_name -> identity_pool_id (only for Cognito mode)
	if profile.IdentityPoolID == "" && profile.IdentityPoolName != "" && profile.FederatedRoleARN == "" {
		profile.IdentityPoolID = profile.IdentityPoolName
	}

	// Defaults
	if profile.AWSRegion == "" {
		profile.AWSRegion = "us-east-1"
	}
	if profile.ProviderType == "" {
		profile.ProviderType = "auto"
	}
	if profile.CredentialStorage == "" {
		profile.CredentialStorage = "session"
	}

	// Auto-detect federation type
	if profile.FederationType == "" {
		if profile.FederatedRoleARN != "" {
			profile.FederationType = "direct"
		} else {
			profile.FederationType = "cognito"
		}
	}

	if profile.MaxSessionDuration == 0 {
		if profile.FederationType == "direct" {
			profile.MaxSessionDuration = 43200
		} else {
			profile.MaxSessionDuration = 28800
		}
	}

	// Only the explicit value "open" may weaken enforcement. Empty, legacy,
	// misspelled, or otherwise unknown values fail closed.
	if profile.QuotaFailMode != "open" {
		profile.QuotaFailMode = "closed"
	}
	if profile.QuotaCheckInterval == 0 {
		profile.QuotaCheckInterval = 30
	}
	if profile.QuotaCheckTimeout == 0 {
		profile.QuotaCheckTimeout = 5
	}
	if profile.GatewayDeploymentMode == "" {
		profile.GatewayDeploymentMode = "development"
	}
	if profile.GatewayDesiredCount == 0 {
		profile.GatewayDesiredCount = 2
	}
	if profile.WebsearchDeploymentMode == "" {
		profile.WebsearchDeploymentMode = "development"
	}

	return &profile, nil
}

// AutoDetectProfile returns the profile name if config.json has exactly one profile.
func AutoDetectProfile() string {
	data, err := readConfigFile()
	if err != nil {
		return ""
	}

	var raw map[string]json.RawMessage
	if err := json.Unmarshal(data, &raw); err != nil {
		return ""
	}

	profilesRaw, ok := raw["profiles"]
	if !ok {
		return ""
	}
	var profiles map[string]json.RawMessage
	if err := json.Unmarshal(profilesRaw, &profiles); err != nil {
		return ""
	}
	if len(profiles) == 1 {
		for name := range profiles {
			return name
		}
	}
	return ""
}

func readConfigFile() ([]byte, error) {
	// Try binary directory first
	exePath, err := os.Executable()
	if err == nil {
		p := filepath.Join(filepath.Dir(exePath), "config.json")
		if data, err := os.ReadFile(p); err == nil {
			return data, nil
		}
	}

	// Fall back to ~/gip/config.json
	home, err := os.UserHomeDir()
	if err != nil {
		return nil, fmt.Errorf("cannot determine home directory: %w", err)
	}
	p := filepath.Join(home, "gip", "config.json")
	data, err := os.ReadFile(p)
	if err != nil {
		return nil, fmt.Errorf("config.json not found: %w", err)
	}
	return data, nil
}

// CredentialProcessPath returns the expected path to the credential-process binary.
func CredentialProcessPath() string {
	home, err := os.UserHomeDir()
	if err != nil {
		return "credential-process"
	}
	name := "credential-process"
	if runtime.GOOS == "windows" {
		name = "credential-process.exe"
	}
	return filepath.Join(home, "gip", name)
}

// IsSsoEnabled reports whether the profile uses SSO/OIDC authentication.
// Defaults to true when the field is absent, preserving behavior for legacy
// bundles that predate PR #303.
func (p *ProfileConfig) IsSsoEnabled() bool {
	if p == nil || p.SsoEnabled == nil {
		return true
	}
	return *p.SsoEnabled
}

// IsIDC reports whether the profile uses IAM Identity Center active auth.
// True when auth_type is explicitly "idc", or when SSO is disabled but IDC
// fields are present (indicating an IDC deployment rather than pure passthrough).
func (p *ProfileConfig) IsIDC() bool {
	if p == nil {
		return false
	}
	return p.AuthType == "idc" || (!p.IsSsoEnabled() && p.IDCStartURL != "")
}
