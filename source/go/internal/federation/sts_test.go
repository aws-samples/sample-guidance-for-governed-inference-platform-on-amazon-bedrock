package federation

import (
	"strings"
	"testing"

	"gip-go/internal/jwt"
)

func TestBuildSessionNameLegacy(t *testing.T) {
	tests := []struct {
		name    string
		binding string
		claims  jwt.Claims
		want    string
	}{
		{
			name:   "email preferred over sub",
			claims: jwt.Claims{"email": "alice@acme.com", "sub": "00u123"},
			want:   "alice@acme.com",
		},
		{
			name:   "email with plus is preserved",
			claims: jwt.Claims{"email": "a.b+filter@example.co.uk"},
			want:   "a.b+filter@example.co.uk",
		},
		{
			name:   "email with invalid chars is sanitized to hyphens",
			claims: jwt.Claims{"email": "first name/ext@example.com"},
			want:   "first-name-ext@example.com",
		},
		{
			name:   "email over 64 chars is truncated",
			claims: jwt.Claims{"email": "a-very-long-local-part-that-exceeds-the-sixty-four-character-session-name-limit@example.com"},
			// 64-char cap, regardless of @ position
			want: "a-very-long-local-part-that-exceeds-the-sixty-four-character-ses",
		},
		{
			name:   "sub fallback when email missing, pipe sanitized",
			claims: jwt.Claims{"sub": "auth0|507f191e810c19729de860ea"},
			want:   "gip-auth0-507f191e810c19729de860ea",
		},
		{
			name:   "sub fallback truncated to 32 chars",
			claims: jwt.Claims{"sub": "this-sub-is-definitely-longer-than-thirty-two-characters"},
			want:   "gip-this-sub-is-definitely-longer-th",
		},
		{
			name:   "no identifying claims yields default",
			claims: jwt.Claims{},
			want:   "gip",
		},
		{
			name:   "empty email falls through to sub",
			claims: jwt.Claims{"email": "", "sub": "user-123"},
			want:   "gip-user-123",
		},
		{
			name:    "explicit none behaves like legacy",
			binding: "none",
			claims:  jwt.Claims{"email": "first name/ext@example.com"},
			want:    "first-name-ext@example.com",
		},
		{
			name:    "explicit legacy behaves like legacy",
			binding: "legacy",
			claims:  jwt.Claims{"sub": "auth0|507f191e810c19729de860ea"},
			want:    "gip-auth0-507f191e810c19729de860ea",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := buildSessionName(tc.claims, tc.binding)
			if err != nil {
				t.Fatalf("buildSessionName(%v, %q) unexpected error: %v", tc.claims, tc.binding, err)
			}
			if got != tc.want {
				t.Errorf("buildSessionName(%v, %q) = %q, want %q", tc.claims, tc.binding, got, tc.want)
			}
			if len(got) > 64 {
				t.Errorf("session name %q exceeds STS 64-char limit (len=%d)", got, len(got))
			}
			if strings.ContainsAny(got, " /\\?#") {
				t.Errorf("session name %q contains characters STS would reject", got)
			}
		})
	}
}

// Bound modes: the trust policy compares the session name to the raw claim
// with StringEquals, so the client must pass the claim through verbatim and
// fail closed on anything STS cannot accept.
func TestBuildSessionNameBound(t *testing.T) {
	tests := []struct {
		name        string
		binding     string
		claims      jwt.Claims
		want        string
		wantErrPart string
	}{
		{
			name:    "email mode passes raw email through",
			binding: "email",
			claims:  jwt.Claims{"email": "alice@acme.com", "sub": "00u123"},
			want:    "alice@acme.com",
		},
		{
			name:    "email mode preserves plus addressing",
			binding: "email",
			claims:  jwt.Claims{"email": "a.b+filter@example.co.uk"},
			want:    "a.b+filter@example.co.uk",
		},
		{
			name:        "email mode fails closed on invalid characters",
			binding:     "email",
			claims:      jwt.Claims{"email": "first name/ext@example.com"},
			wantErrPart: "cannot be an STS session name",
		},
		{
			name:        "email mode fails closed when over 64 chars",
			binding:     "email",
			claims:      jwt.Claims{"email": "a-very-long-local-part-that-exceeds-the-sixty-four-character-session-name-limit@example.com"},
			wantErrPart: "cannot be an STS session name",
		},
		{
			name:        "email mode fails closed when claim missing",
			binding:     "email",
			claims:      jwt.Claims{"sub": "00u123"},
			wantErrPart: `no "email" claim`,
		},
		{
			name:    "sub mode passes raw sub without prefix or truncation",
			binding: "sub",
			claims:  jwt.Claims{"email": "alice@acme.com", "sub": "this-sub-is-definitely-longer-than-thirty-two-characters"},
			want:    "this-sub-is-definitely-longer-than-thirty-two-characters",
		},
		{
			name:        "sub mode fails closed on pipe-delimited auth0 sub",
			binding:     "sub",
			claims:      jwt.Claims{"sub": "auth0|507f191e810c19729de860ea"},
			wantErrPart: "cannot be an STS session name",
		},
		{
			name:        "sub mode fails closed when claim missing",
			binding:     "sub",
			claims:      jwt.Claims{"email": "alice@acme.com"},
			wantErrPart: `no "sub" claim`,
		},
		{
			name:        "email mode fails closed on single-char value",
			binding:     "email",
			claims:      jwt.Claims{"email": "a"},
			wantErrPart: "cannot be an STS session name",
		},
		{
			name:        "unknown binding is rejected",
			binding:     "e-mail",
			claims:      jwt.Claims{"email": "alice@acme.com"},
			wantErrPart: "invalid session_name_binding",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := buildSessionName(tc.claims, tc.binding)
			if tc.wantErrPart != "" {
				if err == nil {
					t.Fatalf("buildSessionName(%v, %q) = %q, expected error containing %q",
						tc.claims, tc.binding, got, tc.wantErrPart)
				}
				if !strings.Contains(err.Error(), tc.wantErrPart) {
					t.Errorf("error %q does not contain %q", err.Error(), tc.wantErrPart)
				}
				return
			}
			if err != nil {
				t.Fatalf("buildSessionName(%v, %q) unexpected error: %v", tc.claims, tc.binding, err)
			}
			if got != tc.want {
				t.Errorf("buildSessionName(%v, %q) = %q, want %q", tc.claims, tc.binding, got, tc.want)
			}
		})
	}
}
