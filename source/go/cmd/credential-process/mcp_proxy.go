package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"

	"gip-go/internal/storage"
)

// --mcp-proxy: a stdio<->streamable-HTTP MCP proxy for harnesses whose MCP
// clients cannot refresh HTTP headers (OpenCode, Codex CLI — R10 verdicts).
// The harness spawns `credential-process --profile <name> --mcp-proxy <url>`
// as a local stdio MCP server; every newline-delimited JSON-RPC message read
// from stdin is POSTed to the AgentCore Gateway MCP endpoint with a FRESH
// Authorization header (same browserless token path as --get-mcp-auth-header),
// and the response is relayed back on stdout. Scope is deliberately minimal:
// request/response JSON-RPC pass-through only — no server-push (SSE streams
// are drained per-response), no sampling, no elicitation. The websearch
// gateway is a plain request/response MCP server, so this covers it fully.

// maxMCPMessageSize bounds a single newline-delimited JSON-RPC message in
// either direction (tool arguments and search results can be large).
const maxMCPMessageSize = 10 * 1024 * 1024

// mcpHTTPTimeout bounds one gateway round-trip (a web search tool call).
const mcpHTTPTimeout = 120 * time.Second

// JSON-RPC error codes emitted by the proxy itself (server-error range).
const (
	mcpErrAuth    = -32000 // browserless token acquisition failed
	mcpErrGateway = -32001 // gateway unreachable or returned an HTTP error
)

// mcpBearerToken returns a gateway-ready bearer token via the same browserless
// path as --get-mcp-auth-header: cached monitoring token first, silent
// refresh_token exchange on a miss. force skips the cache — used after the
// gateway rejects a request with 401/403, which means the cached token is no
// longer accepted. Never opens a browser.
func (a *credentialApp) mcpBearerToken(force bool) (string, error) {
	if !force {
		if token, err := storage.GetMonitoringToken(a.profile, a.cfg.CredentialStorage); err == nil && token != "" {
			return token, nil
		}
	}
	if a.cfg.IsSsoEnabled() && !a.cfg.IsIDC() {
		if auth := a.tryRefreshToken(); auth != nil {
			return auth.IDToken, nil
		}
	}
	return "", fmt.Errorf("no valid cached token for profile '%s'; run the credential process once to authenticate", a.profile)
}

// runMCPProxy wires the proxy loop to stdin/stdout and the app's token path.
func (a *credentialApp) runMCPProxy(gatewayURL string) int {
	if gatewayURL == "" {
		fmt.Fprintln(os.Stderr, "Error: --mcp-proxy requires the gateway MCP URL (e.g. --mcp-proxy https://gw-x.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp)")
		return 1
	}
	proxy := &mcpProxy{
		url:    gatewayURL,
		client: &http.Client{Timeout: mcpHTTPTimeout},
		token:  a.mcpBearerToken,
	}
	return proxy.run(os.Stdin, os.Stdout)
}

// mcpProxy holds the per-process proxy state. token is injectable for tests.
type mcpProxy struct {
	url       string
	client    *http.Client
	token     func(force bool) (string, error)
	sessionID string // Mcp-Session-Id echoed back to the gateway once assigned
}

// run reads newline-delimited JSON-RPC messages from in until EOF, forwarding
// each to the gateway. Returns the process exit code: 0 on clean EOF, non-zero
// on unrecoverable auth failure or a broken stdout pipe (fail closed).
func (p *mcpProxy) run(in io.Reader, out io.Writer) int {
	scanner := bufio.NewScanner(in)
	scanner.Buffer(make([]byte, 64*1024), maxMCPMessageSize)
	for scanner.Scan() {
		line := bytes.TrimSpace(scanner.Bytes())
		if len(line) == 0 {
			continue
		}
		// Scanner reuses its buffer; copy before the next Scan.
		msg := append([]byte(nil), line...)
		if code, terminate := p.forward(msg, out); terminate {
			return code
		}
	}
	if err := scanner.Err(); err != nil {
		fmt.Fprintf(os.Stderr, "Error: reading MCP stdin: %v\n", err)
		return 1
	}
	return 0
}

// forward posts one JSON-RPC message to the gateway with a fresh bearer token
// and relays the response to out. On 401/403 it force-refreshes the token and
// retries exactly once. Returns (exitCode, terminate): terminate is true only
// for unrecoverable failures (browserless auth exhausted, stdout gone) —
// per-request gateway errors are mapped to JSON-RPC errors and the loop
// continues.
func (p *mcpProxy) forward(msg []byte, out io.Writer) (int, bool) {
	id := jsonrpcID(msg)

	token, err := p.token(false)
	if err != nil {
		// No cached token and the silent refresh failed: nothing more a
		// browserless proxy can do. Fail closed (matches quota-blocked
		// behavior) after answering the pending request.
		p.writeError(out, id, mcpErrAuth, err.Error())
		return 1, true
	}

	resp, err := p.post(msg, token)
	if err != nil {
		p.writeError(out, id, mcpErrGateway, fmt.Sprintf("gateway request failed: %v", err))
		return 0, false
	}
	if resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden {
		drainAndClose(resp)
		token, err = p.token(true)
		if err != nil {
			p.writeError(out, id, mcpErrAuth, err.Error())
			return 1, true
		}
		resp, err = p.post(msg, token)
		if err != nil {
			p.writeError(out, id, mcpErrGateway, fmt.Sprintf("gateway request failed: %v", err))
			return 0, false
		}
	}
	defer drainAndClose(resp)

	// Pass through the gateway's session semantics: capture the session id
	// assigned on initialize and echo it on every subsequent request.
	if sid := resp.Header.Get("Mcp-Session-Id"); sid != "" {
		p.sessionID = sid
	}

	switch {
	case resp.StatusCode == http.StatusAccepted || resp.StatusCode == http.StatusNoContent:
		// Notification/response accepted; nothing to relay per streamable HTTP.
		return 0, false
	case resp.StatusCode < 200 || resp.StatusCode >= 300:
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		p.writeError(out, id, mcpErrGateway,
			fmt.Sprintf("gateway returned HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(body))))
		return 0, false
	}

	if err := p.relayBody(resp, out); err != nil {
		fmt.Fprintf(os.Stderr, "Error: writing MCP response to stdout: %v\n", err)
		return 1, true
	}
	return 0, false
}

// post sends one JSON-RPC message as a streamable-HTTP request.
func (p *mcpProxy) post(msg []byte, token string) (*http.Response, error) {
	req, err := http.NewRequest(http.MethodPost, p.url, bytes.NewReader(msg))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json, text/event-stream")
	req.Header.Set("Authorization", "Bearer "+token)
	if p.sessionID != "" {
		req.Header.Set("Mcp-Session-Id", p.sessionID)
	}
	return p.client.Do(req)
}

// relayBody writes the gateway response to out as newline-delimited JSON.
// Streamable HTTP servers may answer either application/json (single message)
// or text/event-stream (one or more messages as SSE events); both are
// normalized to compact single-line JSON so the stdio framing stays intact.
// Returns an error only when out itself fails (broken pipe → terminate).
func (p *mcpProxy) relayBody(resp *http.Response, out io.Writer) error {
	if strings.HasPrefix(resp.Header.Get("Content-Type"), "text/event-stream") {
		return relaySSE(resp.Body, out)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxMCPMessageSize))
	if err != nil {
		fmt.Fprintf(os.Stderr, "Warning: reading gateway response failed: %v\n", err)
		return nil
	}
	return writeCompactJSON(out, body)
}

// relaySSE extracts JSON-RPC messages from an SSE stream (data: lines grouped
// per event) and writes each as one stdout line. Non-JSON events are dropped
// with a stderr warning; the gateway only pushes JSON-RPC payloads.
func relaySSE(r io.Reader, out io.Writer) error {
	scanner := bufio.NewScanner(r)
	scanner.Buffer(make([]byte, 64*1024), maxMCPMessageSize)
	var data []byte
	flush := func() error {
		if len(data) == 0 {
			return nil
		}
		msg := data
		data = nil
		return writeCompactJSON(out, msg)
	}
	for scanner.Scan() {
		line := scanner.Text()
		if line == "" {
			if err := flush(); err != nil {
				return err
			}
			continue
		}
		if rest, ok := strings.CutPrefix(line, "data:"); ok {
			rest = strings.TrimPrefix(rest, " ")
			if len(data) > 0 {
				data = append(data, '\n')
			}
			data = append(data, rest...)
		}
		// event:/id:/retry: fields are irrelevant for request/response relay.
	}
	if err := flush(); err != nil {
		return err
	}
	if err := scanner.Err(); err != nil {
		fmt.Fprintf(os.Stderr, "Warning: reading gateway SSE stream failed: %v\n", err)
	}
	return nil
}

// writeCompactJSON writes body to out as exactly one compact JSON line (MCP
// stdio framing forbids embedded newlines). Empty or non-JSON bodies are
// skipped with a stderr warning; only out write failures are returned.
func writeCompactJSON(out io.Writer, body []byte) error {
	body = bytes.TrimSpace(body)
	if len(body) == 0 {
		return nil
	}
	var buf bytes.Buffer
	if err := json.Compact(&buf, body); err != nil {
		fmt.Fprintf(os.Stderr, "Warning: dropping non-JSON gateway payload (%d bytes)\n", len(body))
		return nil
	}
	buf.WriteByte('\n')
	_, err := out.Write(buf.Bytes())
	return err
}

// jsonrpcID extracts the "id" of a JSON-RPC message as raw JSON. Returns nil
// for notifications (no id) or unparseable input.
func jsonrpcID(msg []byte) json.RawMessage {
	var envelope struct {
		ID json.RawMessage `json:"id"`
	}
	if err := json.Unmarshal(msg, &envelope); err != nil {
		return nil
	}
	return envelope.ID
}

// writeError emits a JSON-RPC error response for the given request id and
// logs the message to stderr. Notifications (id absent/null) get no response
// per JSON-RPC 2.0 — the stderr line is the only trace.
func (p *mcpProxy) writeError(out io.Writer, id json.RawMessage, code int, message string) {
	fmt.Fprintf(os.Stderr, "Error: %s\n", message)
	if len(id) == 0 || string(id) == "null" {
		return
	}
	resp := map[string]interface{}{
		"jsonrpc": "2.0",
		"id":      id,
		"error":   map[string]interface{}{"code": code, "message": message},
	}
	raw, err := json.Marshal(resp)
	if err != nil {
		return
	}
	_, _ = out.Write(append(raw, '\n'))
}

// drainAndClose discards any unread response body so the HTTP connection can
// be reused, then closes it.
func drainAndClose(resp *http.Response) {
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 64*1024))
	_ = resp.Body.Close()
}
