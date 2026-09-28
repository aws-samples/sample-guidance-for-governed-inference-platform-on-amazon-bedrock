package config

import (
	"encoding/json"
	"testing"
)

func TestDeploymentReadinessMinimalConfigDefaultsRoundTrip(t *testing.T) {
	raw := []byte(`{"profiles":{"gip":{"provider_domain":"company.okta.com","client_id":"client-123"}}}`)
	profile, err := parseProfile(raw, "gip")
	if err != nil {
		t.Fatalf("parseProfile failed: %v", err)
	}

	if profile.GatewayDeploymentMode != "development" {
		t.Errorf("GatewayDeploymentMode = %q, want development", profile.GatewayDeploymentMode)
	}
	if profile.GatewayDesiredCount != 2 {
		t.Errorf("GatewayDesiredCount = %d, want 2", profile.GatewayDesiredCount)
	}
	if profile.WebsearchDeploymentMode != "development" {
		t.Errorf("WebsearchDeploymentMode = %q, want development", profile.WebsearchDeploymentMode)
	}
	if profile.GatewayImageDigest != "" || profile.GatewayDBMultiAZ || profile.GatewayDBDeletionProtection || profile.WebsearchPolicyValidationComplete {
		t.Errorf("legacy readiness fields did not retain safe zero values: %+v", profile)
	}

	encoded, err := json.Marshal(profile)
	if err != nil {
		t.Fatalf("json.Marshal failed: %v", err)
	}
	var restored ProfileConfig
	if err := json.Unmarshal(encoded, &restored); err != nil {
		t.Fatalf("json.Unmarshal failed: %v", err)
	}
	if restored.GatewayDeploymentMode != "development" || restored.GatewayDesiredCount != 2 || restored.WebsearchDeploymentMode != "development" {
		t.Errorf("defaulted readiness values did not survive JSON round trip: %+v", restored)
	}
}

func TestDeploymentReadinessProductionValuesRoundTrip(t *testing.T) {
	want := ProfileConfig{
		GatewayDeploymentMode:             "production",
		GatewayImageDigest:                "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
		GatewayDBMultiAZ:                  true,
		GatewayDBDeletionProtection:       true,
		GatewayDesiredCount:               3,
		WebsearchDeploymentMode:           "production",
		WebsearchPolicyValidationComplete: true,
	}
	raw := []byte(`{"profiles":{"gip":{"gateway_deployment_mode":"production","gateway_image_digest":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","gateway_db_multi_az":true,"gateway_db_deletion_protection":true,"gateway_desired_count":3,"websearch_deployment_mode":"production","websearch_policy_validation_complete":true}}}`)
	profile, err := parseProfile(raw, "gip")
	if err != nil {
		t.Fatalf("parseProfile failed: %v", err)
	}

	if profile.GatewayDeploymentMode != want.GatewayDeploymentMode ||
		profile.GatewayImageDigest != want.GatewayImageDigest ||
		profile.GatewayDBMultiAZ != want.GatewayDBMultiAZ ||
		profile.GatewayDBDeletionProtection != want.GatewayDBDeletionProtection ||
		profile.GatewayDesiredCount != want.GatewayDesiredCount ||
		profile.WebsearchDeploymentMode != want.WebsearchDeploymentMode ||
		profile.WebsearchPolicyValidationComplete != want.WebsearchPolicyValidationComplete {
		t.Fatalf("production readiness values changed during JSON round trip: got %+v, want %+v", profile, want)
	}

	encoded, err := json.Marshal(profile)
	if err != nil {
		t.Fatalf("json.Marshal failed: %v", err)
	}
	var serialized map[string]any
	if err := json.Unmarshal(encoded, &serialized); err != nil {
		t.Fatalf("json.Unmarshal failed: %v", err)
	}
	for key, expected := range map[string]any{
		"gateway_deployment_mode":              "production",
		"gateway_image_digest":                 want.GatewayImageDigest,
		"gateway_db_multi_az":                  true,
		"gateway_db_deletion_protection":       true,
		"gateway_desired_count":                float64(3),
		"websearch_deployment_mode":            "production",
		"websearch_policy_validation_complete": true,
	} {
		if serialized[key] != expected {
			t.Errorf("serialized[%q] = %#v, want %#v", key, serialized[key], expected)
		}
	}
}
