package config

import (
	"os"
	"path/filepath"
	"testing"
)

func TestInvalidQuotaFailModeDefaultsClosed(t *testing.T) {
	path := filepath.Join(t.TempDir(), "config.json")
	data := []byte(`{"profiles":{"gip":{"quota_fail_mode":"Open"}}}`)
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatalf("write config: %v", err)
	}

	profile, err := LoadProfileFromPath(path, "gip")
	if err != nil {
		t.Fatalf("LoadProfileFromPath: %v", err)
	}
	if profile.QuotaFailMode != "closed" {
		t.Fatalf("QuotaFailMode = %q, want closed", profile.QuotaFailMode)
	}
}
