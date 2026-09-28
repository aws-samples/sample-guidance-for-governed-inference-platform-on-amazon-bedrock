package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"gip-go/internal/config"
	"gip-go/internal/storage"
)

// staticToken returns a token func that always yields tok and records calls.
func staticToken(tok string) func(force bool) (string, error) {
	return func(force bool) (string, error) { return tok, nil }
}

// newProxy builds an mcpProxy against the given test server URL.
func newProxy(url string, token func(force bool) (string, error)) *mcpProxy {
	return &mcpProxy{
		url:    url,
		client: &http.Client{Timeout: 5 * time.Second},
		token:  token,
	}
}

// runProxy feeds input lines through the proxy loop and returns exit code + stdout.
func runProxy(t *testing.T, p *mcpProxy, input string) (int, string) {
	t.Helper()
	var out bytes.Buffer
	code := p.run(strings.NewReader(input), &out)
	return code, out.String()
}

// TestMCPProxy_PassThrough pins the core contract: one JSON-RPC request in,
// POSTed to the gateway with Authorization/Content-Type/Accept headers, and the
// gateway's JSON response relayed to stdout as exactly one compact line (the
// pretty-printed server body must be compacted — stdio framing forbids
// embedded newlines).
func TestMCPProxy_PassThrough(t *testing.T) {
	var gotAuth, gotContentType, gotAccept, gotBody string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		gotContentType = r.Header.Get("Content-Type")
		gotAccept = r.Header.Get("Accept")
		body, _ := io.ReadAll(r.Body)
		gotBody = string(body)
		w.Header().Set("Content-Type", "application/json")
		// Deliberately pretty-printed: the proxy must compact it.
		_, _ = w.Write([]byte("{\n  \"jsonrpc\": \"2.0\",\n  \"id\": 1,\n  \"result\": {\"tools\": []}\n}"))
	}))
	defer srv.Close()

	request := `{"jsonrpc":"2.0","id":1,"method":"tools/list"}`
	code, out := runProxy(t, newProxy(srv.URL, staticToken("cached-tok")), request+"\n")

	if code != 0 {
		t.Fatalf("run exit = %d, want 0", code)
	}
	if gotAuth != "Bearer cached-tok" {
		t.Errorf("Authorization = %q, want %q", gotAuth, "Bearer cached-tok")
	}
	if gotContentType != "application/json" {
		t.Errorf("Content-Type = %q, want application/json", gotContentType)
	}
	if gotAccept != "application/json, text/event-stream" {
		t.Errorf("Accept = %q, want both media types per streamable HTTP", gotAccept)
	}
	if gotBody != request {
		t.Errorf("forwarded body = %q, want the raw JSON-RPC message %q", gotBody, request)
	}
	const want = `{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}` + "\n"
	if out != want {
		t.Errorf("stdout = %q, want single compact line %q", out, want)
	}
}

// TestMCPProxy_RefreshOn401 verifies the 401 path: the proxy must force-refresh
// the token (force=true) and retry the request exactly once with the fresh one.
func TestMCPProxy_RefreshOn401(t *testing.T) {
	var requests int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests++
		if r.Header.Get("Authorization") != "Bearer fresh-tok" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"jsonrpc":"2.0","id":7,"result":{}}`))
	}))
	defer srv.Close()

	var forcedCalls int
	token := func(force bool) (string, error) {
		if force {
			forcedCalls++
			return "fresh-tok", nil
		}
		return "stale-tok", nil
	}

	code, out := runProxy(t, newProxy(srv.URL, token), `{"jsonrpc":"2.0","id":7,"method":"tools/call"}`+"\n")
	if code != 0 {
		t.Fatalf("run exit = %d, want 0", code)
	}
	if forcedCalls != 1 {
		t.Errorf("forced token refreshes = %d, want exactly 1", forcedCalls)
	}
	if requests != 2 {
		t.Errorf("gateway requests = %d, want 2 (401 then retry)", requests)
	}
	if want := `{"jsonrpc":"2.0","id":7,"result":{}}` + "\n"; out != want {
		t.Errorf("stdout = %q, want %q", out, want)
	}
}

// TestMCPProxy_SessionIDPropagation verifies Mcp-Session-Id pass-through: the
// id assigned by the gateway on the first response must be echoed as a request
// header on subsequent requests (the gateway's session semantics survive the
// proxy).
func TestMCPProxy_SessionIDPropagation(t *testing.T) {
	var sessionHeaders []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		sessionHeaders = append(sessionHeaders, r.Header.Get("Mcp-Session-Id"))
		w.Header().Set("Mcp-Session-Id", "sess-123")
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"jsonrpc":"2.0","id":1,"result":{}}`))
	}))
	defer srv.Close()

	input := `{"jsonrpc":"2.0","id":1,"method":"initialize"}` + "\n" +
		`{"jsonrpc":"2.0","id":2,"method":"tools/list"}` + "\n"
	code, _ := runProxy(t, newProxy(srv.URL, staticToken("tok")), input)
	if code != 0 {
		t.Fatalf("run exit = %d, want 0", code)
	}
	if len(sessionHeaders) != 2 {
		t.Fatalf("gateway requests = %d, want 2", len(sessionHeaders))
	}
	if sessionHeaders[0] != "" {
		t.Errorf("first request Mcp-Session-Id = %q, want empty (not yet assigned)", sessionHeaders[0])
	}
	if sessionHeaders[1] != "sess-123" {
		t.Errorf("second request Mcp-Session-Id = %q, want sess-123", sessionHeaders[1])
	}
}

// TestMCPProxy_NotificationProducesNoOutput verifies notifications (no id) are
// forwarded but produce no stdout when the gateway answers 202 Accepted.
func TestMCPProxy_NotificationProducesNoOutput(t *testing.T) {
	var requests int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests++
		w.WriteHeader(http.StatusAccepted)
	}))
	defer srv.Close()

	code, out := runProxy(t, newProxy(srv.URL, staticToken("tok")),
		`{"jsonrpc":"2.0","method":"notifications/initialized"}`+"\n")
	if code != 0 {
		t.Fatalf("run exit = %d, want 0", code)
	}
	if requests != 1 {
		t.Errorf("gateway requests = %d, want 1 (notification must still be forwarded)", requests)
	}
	if out != "" {
		t.Errorf("stdout = %q, want empty for an accepted notification", out)
	}
}

// TestMCPProxy_HTTPErrorMapsToJSONRPCError verifies a non-2xx gateway response
// is mapped to a JSON-RPC error carrying the SAME request id, and the proxy
// keeps serving (exit 0 at EOF) — one failed call must not kill the session.
func TestMCPProxy_HTTPErrorMapsToJSONRPCError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "boom", http.StatusInternalServerError)
	}))
	defer srv.Close()

	code, out := runProxy(t, newProxy(srv.URL, staticToken("tok")),
		`{"jsonrpc":"2.0","id":42,"method":"tools/call"}`+"\n")
	if code != 0 {
		t.Fatalf("run exit = %d, want 0 (per-request errors must not terminate)", code)
	}
	var resp struct {
		Jsonrpc string `json:"jsonrpc"`
		ID      int    `json:"id"`
		Error   struct {
			Code    int    `json:"code"`
			Message string `json:"message"`
		} `json:"error"`
	}
	if err := json.Unmarshal([]byte(out), &resp); err != nil {
		t.Fatalf("stdout not a JSON-RPC message: %v (raw=%q)", err, out)
	}
	if resp.ID != 42 {
		t.Errorf("error response id = %d, want 42", resp.ID)
	}
	if resp.Error.Code != mcpErrGateway {
		t.Errorf("error code = %d, want %d", resp.Error.Code, mcpErrGateway)
	}
	if !strings.Contains(resp.Error.Message, "HTTP 500") || !strings.Contains(resp.Error.Message, "boom") {
		t.Errorf("error message = %q, want HTTP status + body", resp.Error.Message)
	}
}

// TestMCPProxy_SSEResponse verifies a text/event-stream response is unpacked:
// each SSE event's data becomes one compact JSON line on stdout.
func TestMCPProxy_SSEResponse(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = w.Write([]byte("event: message\ndata: {\"jsonrpc\":\"2.0\",\"id\":3,\"result\":{\"content\":[]}}\n\n"))
	}))
	defer srv.Close()

	code, out := runProxy(t, newProxy(srv.URL, staticToken("tok")),
		`{"jsonrpc":"2.0","id":3,"method":"tools/call"}`+"\n")
	if code != 0 {
		t.Fatalf("run exit = %d, want 0", code)
	}
	if want := `{"jsonrpc":"2.0","id":3,"result":{"content":[]}}` + "\n"; out != want {
		t.Errorf("stdout = %q, want %q", out, want)
	}
}

// TestMCPProxy_AuthFailureFailsClosed verifies the R10 contract: when
// browserless token acquisition is exhausted, the proxy answers the pending
// request with a JSON-RPC error and exits non-zero (matches quota-blocked
// behavior; never hangs, never opens a browser).
func TestMCPProxy_AuthFailureFailsClosed(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Error("gateway must not be called when no token can be acquired")
	}))
	defer srv.Close()

	token := func(force bool) (string, error) {
		return "", fmt.Errorf("no valid cached token for profile 'p'; run the credential process once to authenticate")
	}
	code, out := runProxy(t, newProxy(srv.URL, token),
		`{"jsonrpc":"2.0","id":9,"method":"tools/list"}`+"\n")
	if code != 1 {
		t.Fatalf("run exit = %d, want 1 (fail closed on unrecoverable auth failure)", code)
	}
	var resp struct {
		ID    int `json:"id"`
		Error struct {
			Code int `json:"code"`
		} `json:"error"`
	}
	if err := json.Unmarshal([]byte(out), &resp); err != nil {
		t.Fatalf("stdout not a JSON-RPC message: %v (raw=%q)", err, out)
	}
	if resp.ID != 9 || resp.Error.Code != mcpErrAuth {
		t.Errorf("error response = %+v, want id=9 code=%d", resp, mcpErrAuth)
	}
}

// TestMCPProxy_EmptyLinesAndEOF verifies blank stdin lines are skipped and a
// clean EOF exits 0 without touching the gateway.
func TestMCPProxy_EmptyLinesAndEOF(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Error("gateway must not be called for blank input")
	}))
	defer srv.Close()

	code, out := runProxy(t, newProxy(srv.URL, staticToken("tok")), "\n  \n\n")
	if code != 0 {
		t.Fatalf("run exit = %d, want 0", code)
	}
	if out != "" {
		t.Errorf("stdout = %q, want empty", out)
	}
}

// TestMCPBearerToken_CachedToken verifies the proxy's token source hits the
// same cached-monitoring-token fast path as --get-mcp-auth-header (shared
// internals, no network).
func TestMCPBearerToken_CachedToken(t *testing.T) {
	tmpDir := t.TempDir()
	t.Setenv("HOME", tmpDir)
	t.Setenv("USERPROFILE", tmpDir)
	t.Setenv("GIP_MONITORING_TOKEN", "")

	profile := "test-mcp-proxy-cached"
	writeMonitoringToken(t, tmpDir, profile, "cached-proxy-token", time.Now().Unix()+3600)

	cfg := &config.ProfileConfig{
		ClientID:          "test-client",
		ProviderDomain:    "test.example.com",
		CredentialStorage: "session",
		SsoEnabled:        boolPtr(true),
	}
	app := &credentialApp{profile: profile, cfg: cfg, providerType: "okta"}

	tok, err := app.mcpBearerToken(false)
	if err != nil {
		t.Fatalf("mcpBearerToken(false) error = %v, want nil", err)
	}
	if tok != "cached-proxy-token" {
		t.Errorf("token = %q, want the cached monitoring token", tok)
	}
}

// TestMCPBearerToken_ForceSkipsCacheAndRefreshes verifies force=true (the 401
// path) bypasses a still-valid cached token and performs the browserless
// refresh_token exchange — same decoupled refreshIDTokenOnly path as
// --get-mcp-auth-header, no AWS credential exchange required.
func TestMCPBearerToken_ForceSkipsCacheAndRefreshes(t *testing.T) {
	tmpDir := t.TempDir()
	t.Setenv("HOME", tmpDir)
	t.Setenv("USERPROFILE", tmpDir)
	t.Setenv("GIP_MONITORING_TOKEN", "")
	if err := os.MkdirAll(filepath.Join(tmpDir, ".gip-session"), 0o700); err != nil {
		t.Fatalf("mkdir: %v", err)
	}

	freshIDToken := fakeJWT(t, map[string]interface{}{
		"email": "user@example.com",
		"exp":   time.Now().Unix() + 3600,
	})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]string{
			"id_token":      freshIDToken,
			"refresh_token": "rotated-refresh-token",
		})
	}))
	defer srv.Close()

	profile := "test-mcp-proxy-force"
	// A valid cached token exists — force must NOT return it.
	writeMonitoringToken(t, tmpDir, profile, "still-valid-but-rejected", time.Now().Unix()+3600)
	if err := storage.SaveRefreshToken(profile, "session", "stored-refresh-token"); err != nil {
		t.Fatalf("seed refresh token: %v", err)
	}
	t.Cleanup(func() { storage.ClearRefreshToken(profile) })

	cfg := &config.ProfileConfig{
		ClientID:          "test-client",
		ProviderDomain:    "generic.example.com",
		CredentialStorage: "session",
		SsoEnabled:        boolPtr(true),
		OIDCTokenEndpoint: srv.URL,
	}
	app := &credentialApp{profile: profile, cfg: cfg, providerType: "generic"}

	tok, err := app.mcpBearerToken(true)
	if err != nil {
		t.Fatalf("mcpBearerToken(true) error = %v, want nil", err)
	}
	if tok != freshIDToken {
		t.Errorf("token = %q, want the freshly refreshed id_token", tok)
	}
}
