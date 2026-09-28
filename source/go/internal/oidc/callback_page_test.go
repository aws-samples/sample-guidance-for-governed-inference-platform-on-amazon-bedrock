package oidc

import (
	"net/http/httptest"
	"strings"
	"testing"
)

// TestSendHTML_RendersMessageEscaped pins the callback page: the status code
// and content type are preserved, the message lands inside <h1>, and markup
// in the message is escaped rather than rendered.
func TestSendHTML_RendersMessageEscaped(t *testing.T) {
	rec := httptest.NewRecorder()
	sendHTML(rec, 400, `Authentication failed <script>alert("x")</script>`)

	if rec.Code != 400 {
		t.Errorf("status = %d, want 400", rec.Code)
	}
	if ct := rec.Header().Get("Content-Type"); ct != "text/html" {
		t.Errorf("Content-Type = %q, want text/html", ct)
	}
	body := rec.Body.String()
	if strings.Contains(body, "<script>") {
		t.Fatalf("unescaped <script> reached the page:\n%s", body)
	}
	want := `<h1>Authentication failed &lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt;</h1>`
	if !strings.Contains(body, want) {
		t.Errorf("body missing %q\n%s", want, body)
	}
	if !strings.Contains(body, "<p>Return to your terminal to continue.</p>") {
		t.Errorf("static page body missing:\n%s", body)
	}
}

func TestSendHTML_SuccessMessage(t *testing.T) {
	rec := httptest.NewRecorder()
	sendHTML(rec, 200, "Authentication successful! You can close this window.")

	if rec.Code != 200 {
		t.Errorf("status = %d, want 200", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), "<h1>Authentication successful! You can close this window.</h1>") {
		t.Errorf("body = %q", rec.Body.String())
	}
}
