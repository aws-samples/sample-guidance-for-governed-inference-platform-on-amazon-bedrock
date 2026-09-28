package main

// ABOUTME: Pins the html/template rendering of the localhost quota-status page.
// ABOUTME: Numbers/colours land in their CSS and text contexts unmodified and the
// ABOUTME: quota API message is escaped so a spoofed endpoint cannot inject markup.

import (
	"bytes"
	"strings"
	"testing"
)

func renderQuotaPage(t *testing.T, usage map[string]interface{}, message string, isBlocked bool) string {
	t.Helper()
	var b bytes.Buffer
	if err := quotaPageTmpl.Execute(&b, buildQuotaPageData(usage, message, isBlocked)); err != nil {
		t.Fatalf("quotaPageTmpl.Execute: %v", err)
	}
	return b.String()
}

func TestQuotaPageWarningRendersValuesInContext(t *testing.T) {
	page := renderQuotaPage(t, warningResult().Usage, "Access granted - enforcement mode is alert-only", false)

	for _, want := range []string{
		"<h1>⚠️ Quota Warning</h1>",
		".header { background: #fff3cd;",
		".header h1 { margin: 0; color: #ffc107;",
		"<span class=\"usage-value\">8,826,347 / 40,000,000 (22%)</span>",
		"style=\"width: 22%; background: #28a745;\"",
		"<span class=\"usage-value\">8,438,308 / 5,500,000 (153%)</span>",
		"style=\"width: 100%; background: #dc3545;\"", // daily bar clamped to 100%, red at >= 100
		"<p class=\"message\">Access granted - enforcement mode is alert-only</p>",
	} {
		if !strings.Contains(page, want) {
			t.Errorf("page missing %q\n%s", want, page)
		}
	}
	// html/template substitutes ZgotmplZ when a value is rejected by its
	// context filter (e.g. a colour string that fails the CSS filter).
	if strings.Contains(page, "ZgotmplZ") {
		t.Errorf("a template value was rejected by html/template:\n%s", page)
	}
}

func TestQuotaPageBlockedPaletteAndNoDailySection(t *testing.T) {
	usage := map[string]interface{}{
		"monthly_percent": float64(85.4),
		"monthly_tokens":  float64(850),
		"monthly_limit":   float64(1000),
	}
	page := renderQuotaPage(t, usage, "", true)

	for _, want := range []string{
		"<h1>🚫 Access Blocked</h1>",
		".header { background: #f8d7da;",
		".header h1 { margin: 0; color: #dc3545;",
		"<span class=\"usage-value\">850 / 1,000 (85%)</span>",
		"style=\"width: 85%; background: #ffc107;\"", // 80 <= pct < 90 is amber
	} {
		if !strings.Contains(page, want) {
			t.Errorf("page missing %q\n%s", want, page)
		}
	}
	if strings.Contains(page, "Daily Usage") {
		t.Errorf("daily section rendered without a daily limit:\n%s", page)
	}
	if strings.Contains(page, "class=\"message\"") {
		t.Errorf("message paragraph rendered for an empty message:\n%s", page)
	}
}

func TestQuotaPageEscapesMessage(t *testing.T) {
	// The message comes from the quota API response body: a compromised or
	// spoofed endpoint must not be able to inject script into the page.
	page := renderQuotaPage(t, warningResult().Usage, `<script>alert(1)</script> & "quotes"`, false)

	if strings.Contains(page, "<script>") {
		t.Fatalf("unescaped <script> reached the page:\n%s", page)
	}
	want := `<p class="message">&lt;script&gt;alert(1)&lt;/script&gt; &amp; &#34;quotes&#34;</p>`
	if !strings.Contains(page, want) {
		t.Errorf("page missing escaped message %q\n%s", want, page)
	}
}
