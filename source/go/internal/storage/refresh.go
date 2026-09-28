package storage

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"time"

	"github.com/99designs/keyring"
)

// refreshTokenData is the on-disk format for a cached refresh token.
type refreshTokenData struct {
	Token     string `json:"refresh_token"`
	Profile   string `json:"profile"`
	UpdatedAt int64  `json:"updated_at"`
}

// SaveRefreshToken persists the OIDC refresh_token for a profile.
// In keyring mode it follows the credential storage contract and stays out of
// the filesystem. Session mode stores it at ~/.gip-session/{profile}-refresh.json
// with user-only permissions (0600).
func SaveRefreshToken(profile, credentialStorage, token string) error {
	if token == "" {
		return nil // IdP didn't issue a refresh token — nothing to store
	}

	data := refreshTokenData{
		Token:     token,
		Profile:   profile,
		UpdatedAt: time.Now().Unix(),
	}

	jsonBytes, err := json.Marshal(data)
	if err != nil {
		return err
	}

	if credentialStorage == "keyring" {
		kr, err := openKeyring()
		if err != nil {
			return err
		}
		return saveRefreshTokenToKeyringImpl(kr, profile, jsonBytes)
	}

	dir := sessionDir()
	if err := os.MkdirAll(dir, 0700); err != nil {
		return err
	}
	path := filepath.Join(dir, profile+"-refresh.json")

	// Write atomically via temp file + rename
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, jsonBytes, 0600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// LoadRefreshToken retrieves the cached refresh_token for a profile.
// Returns empty string if no token is cached (not an error).
func LoadRefreshToken(profile, credentialStorage string) string {
	if credentialStorage == "keyring" {
		kr, err := openKeyring()
		if err == nil {
			if token := loadRefreshTokenFromKeyringImpl(kr, profile); token != "" {
				return token
			}
			if token := loadRefreshTokenFromSession(profile); token != "" {
				_ = SaveRefreshToken(profile, credentialStorage, token)
				return token
			}
		}
		return loadRefreshTokenFromSession(profile)
	}

	return loadRefreshTokenFromSession(profile)
}

func loadRefreshTokenFromSession(profile string) string {
	path := filepath.Join(sessionDir(), profile+"-refresh.json")

	raw, err := os.ReadFile(path)
	if err != nil {
		return ""
	}

	var data refreshTokenData
	if err := json.Unmarshal(raw, &data); err != nil {
		return ""
	}

	return data.Token
}

// ClearRefreshToken removes the cached refresh_token for a profile.
func ClearRefreshToken(profile string) {
	path := filepath.Join(sessionDir(), profile+"-refresh.json")
	os.Remove(path)
	if kr, err := openKeyring(); err == nil {
		_ = kr.Remove(profile + "-refresh")
	}
}

func saveRefreshTokenToKeyringImpl(kr keyringRW, profile string, jsonBytes []byte) error {
	return kr.Set(keyring.Item{Key: profile + "-refresh", Data: jsonBytes})
}

func loadRefreshTokenFromKeyringImpl(kr keyringRW, profile string) string {
	item, err := kr.Get(profile + "-refresh")
	if err != nil {
		if errors.Is(err, keyring.ErrKeyNotFound) {
			return ""
		}
		return ""
	}

	var data refreshTokenData
	if err := json.Unmarshal(item.Data, &data); err != nil {
		return ""
	}
	return data.Token
}

// sessionDir returns the session directory path.
func sessionDir() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".gip-session")
}
