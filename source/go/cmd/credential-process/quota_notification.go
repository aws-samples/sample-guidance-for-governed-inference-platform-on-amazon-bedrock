package main

// ABOUTME: Browser-based quota notification for visual feedback.
// ABOUTME: Serves an HTML page on localhost with progress bars showing quota usage.
// ABOUTME: Parity with Python credential-provider's _show_quota_browser_notification.

import (
	"context"
	"fmt"
	"html/template"
	"net"
	"net/http"
	"os"
	"runtime"
	"sync"
	"time"

	"gip-go/internal/browser"
	"gip-go/internal/quota"
)

// pendingNotification holds a quota-status server that has been started and had
// its browser opened, but whose page may not yet have been fetched.
type pendingNotification struct {
	server *http.Server
	served chan struct{}
	url    string // for tests/diagnostics
}

var (
	pendingMu     sync.Mutex
	pendingNotifs []*pendingNotification

	// notificationWaitTimeout bounds how long the process lingers at exit
	// waiting for the browser to fetch the quota page. Mirrors the Python
	// implementation's server.timeout = 5. Var (not const) so tests can shrink it.
	notificationWaitTimeout = 5 * time.Second

	// openBrowserFunc is the indirection point for opening a browser, so tests
	// can substitute a no-op instead of launching a real browser.
	openBrowserFunc = browser.OpenURL

	// isHeadlessFunc is the indirection point for headless detection, so tests
	// can force a non-headless environment regardless of CI's DISPLAY/SSH vars.
	isHeadlessFunc = isHeadless
)

// showQuotaBrowserNotification starts a localhost server showing quota status
// and opens the browser to it. It does NOT wait for the page to be fetched —
// the caller must invoke waitForQuotaNotification() before the process exits.
//
// Why this split: credential-process exits via os.Exit, which kills every
// goroutine immediately. A fire-and-forget server raced the process exit, so
// the browser frequently hit the socket after it was already gone and showed
// ERR_CONNECTION_REFUSED. By binding the listener synchronously here (the
// socket accepts connections into the kernel backlog the instant net.Listen
// returns) and deferring the wait to the exit path — after credentials are
// already on stdout — the server stays alive until the browser connects,
// without ever delaying credential delivery.
//
// Skipped when:
// - GIP_NO_BROWSER_NOTIFICATION=1 is set
// - Running in a headless environment (no DISPLAY, SSH_CONNECTION set)
// - Usage data is nil
func showQuotaBrowserNotification(qr *quota.Result, isBlocked bool) {
	// Opt-out via environment variable
	if os.Getenv("GIP_NO_BROWSER_NOTIFICATION") == "1" {
		debugPrint("Browser notification skipped (GIP_NO_BROWSER_NOTIFICATION=1)")
		return
	}

	// Skip in headless environments
	if isHeadlessFunc() {
		debugPrint("Browser notification skipped (headless environment detected)")
		return
	}

	usage := qr.Usage
	if usage == nil {
		return
	}

	data := buildQuotaPageData(usage, qr.Message, isBlocked)

	// Bind the listener synchronously (8401 preferred, matching Python). Binding
	// before opening the browser is what closes the connection race: the socket
	// queues the browser's connection even before Serve() is scheduled.
	listener, err := net.Listen("tcp", "127.0.0.1:8401")
	if err != nil {
		// Try any available port
		listener, err = net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			debugPrint("Could not start quota notification server: %v", err)
			return
		}
	}

	port := listener.Addr().(*net.TCPAddr).Port
	served := make(chan struct{})
	var once sync.Once

	mux := http.NewServeMux()
	mux.HandleFunc("/quota-status", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		// html/template escapes every field of data for its context; the only
		// externally sourced value (message, from the quota API body) cannot
		// inject markup or CSS into the page.
		if err := quotaPageTmpl.Execute(w, data); err != nil {
			debugPrint("Could not render quota notification page: %v", err)
		}
		// once: a browser reload would hit this handler again; closing a
		// channel twice panics.
		once.Do(func() { close(served) })
	})

	server := &http.Server{Handler: mux}
	url := fmt.Sprintf("http://127.0.0.1:%d/quota-status", port)
	if err := openBrowserFunc(url); err != nil {
		// Browser errors can include user-controlled launcher details. Keep the
		// diagnostic generic so a callback URL or future query values cannot be
		// copied into credential-process stderr.
		debugPrint("Could not open quota notification browser; notification skipped")
		_ = listener.Close()
		return
	}
	go server.Serve(listener)

	pendingMu.Lock()
	pendingNotifs = append(pendingNotifs, &pendingNotification{server: server, served: served, url: url})
	pendingMu.Unlock()
}

// waitForQuotaNotification blocks until any pending quota page has been fetched
// by the browser (or notificationWaitTimeout elapses), then shuts the server(s)
// down. Call this immediately before the process exits: credentials are already
// on stdout by then, so this only delays process teardown, never the AWS SDK.
func waitForQuotaNotification() {
	pendingMu.Lock()
	notifs := pendingNotifs
	pendingNotifs = nil
	pendingMu.Unlock()

	for _, n := range notifs {
		select {
		case <-n.served:
		case <-time.After(notificationWaitTimeout):
			debugPrint("Browser notification timed out")
		}
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		if err := n.server.Shutdown(ctx); err != nil {
			// Shutdown normally waits only for the in-flight quota response. Fall
			// back to Close after the bound so process teardown cannot hang.
			_ = n.server.Close()
		}
		cancel()
	}
}

// isHeadless returns true if the environment appears to have no usable local
// browser — i.e. we should show a copy-to-another-device prompt rather than try
// to open a browser. Used by both the quota browser-notification flow and the
// IDC device-authorization flow.
func isHeadless() bool {
	// An explicit $BROWSER means the user has told us how to open a browser
	// (e.g. WSL forwarding to the Windows host) — honor it.
	if os.Getenv("BROWSER") != "" {
		return false
	}
	// SSH session — no local display, on any OS.
	if os.Getenv("SSH_CONNECTION") != "" || os.Getenv("SSH_TTY") != "" || os.Getenv("SSH_CLIENT") != "" {
		return true
	}
	switch runtime.GOOS {
	case "windows", "darwin":
		// Assume a desktop browser unless in an SSH session (handled above).
		return false
	default:
		// Linux/BSD: a GUI session exposes DISPLAY (X11) or WAYLAND_DISPLAY.
		return os.Getenv("DISPLAY") == "" && os.Getenv("WAYLAND_DISPLAY") == ""
	}
}

// quotaPageData is the view model for quotaPageTmpl. Colours are fixed palette
// values chosen in code, numbers are pre-formatted strings, and Message is the
// only externally sourced value (quota API response body) — html/template
// escapes it for the HTML text context when the page is rendered.
type quotaPageData struct {
	HeaderBg        string
	StatusColor     string
	StatusEmoji     string
	StatusText      string
	MonthlyTokens   string
	MonthlyLimit    string
	MonthlyPercent  string
	MonthlyWidth    string
	MonthlyBarColor string
	ShowDaily       bool
	DailyTokens     string
	DailyLimit      string
	DailyPercent    string
	DailyWidth      string
	DailyBarColor   string
	Message         string
}

// quotaPageTmpl is the localhost quota-status page. Parsed once at init so a
// malformed template fails the binary immediately rather than at first use.
var quotaPageTmpl = template.Must(template.New("quota-status").Parse(`<!DOCTYPE html>
<html>
<head>
    <title>Quota Status - Governed Inference Platform</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 0; padding: 40px; background: #f5f5f5; }
        .container { max-width: 500px; margin: 0 auto; background: white; border-radius: 12px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); overflow: hidden; }
        .header { background: {{.HeaderBg}}; padding: 30px; text-align: center; border-bottom: 1px solid rgba(0,0,0,0.1); }
        .header h1 { margin: 0; color: {{.StatusColor}}; font-size: 28px; }
        .content { padding: 30px; }
        .usage-section { margin-bottom: 25px; }
        .usage-label { display: flex; justify-content: space-between; margin-bottom: 8px; font-size: 14px; color: #666; }
        .usage-value { font-weight: 600; color: #333; }
        .progress-bar { height: 24px; background: #e9ecef; border-radius: 12px; overflow: hidden; }
        .progress-fill { height: 100%; border-radius: 12px; transition: width 0.3s; }
        .message { color: #666; font-size: 14px; margin-top: 20px; padding: 15px; background: #f8f9fa; border-radius: 8px; }
        .footer { text-align: center; padding: 15px; color: #999; font-size: 12px; }
    </style>
</head>
<body>
<div class="container">
    <div class="header"><h1>{{.StatusEmoji}} {{.StatusText}}</h1></div>
    <div class="content">
        <div class="usage-section">
            <div class="usage-label">
                <span>Monthly Usage</span>
                <span class="usage-value">{{.MonthlyTokens}} / {{.MonthlyLimit}} ({{.MonthlyPercent}}%)</span>
            </div>
            <div class="progress-bar">
                <div class="progress-fill" style="width: {{.MonthlyWidth}}%; background: {{.MonthlyBarColor}};"></div>
            </div>
        </div>
        {{if .ShowDaily}}
		<div class="usage-section">
			<div class="usage-label">
				<span>Daily Usage</span>
				<span class="usage-value">{{.DailyTokens}} / {{.DailyLimit}} ({{.DailyPercent}}%)</span>
			</div>
			<div class="progress-bar">
				<div class="progress-fill" style="width: {{.DailyWidth}}%; background: {{.DailyBarColor}};"></div>
			</div>
		</div>{{end}}
        {{if .Message}}<p class="message">{{.Message}}</p>{{end}}
    </div>
    <div class="footer">Governed Inference Platform on Amazon Bedrock — Quota Monitor</div>
</div>
</body>
</html>`))

func buildQuotaPageData(usage map[string]interface{}, message string, isBlocked bool) quotaPageData {
	monthlyPercent, _ := usage["monthly_percent"].(float64)
	dailyPercent, _ := usage["daily_percent"].(float64)
	monthlyTokens, _ := usage["monthly_tokens"].(float64)
	monthlyLimit, _ := usage["monthly_limit"].(float64)
	dailyTokens, _ := usage["daily_tokens"].(float64)
	dailyLimit, _ := usage["daily_limit"].(float64)

	data := quotaPageData{
		StatusEmoji: "⚠️",
		StatusText:  "Quota Warning",
		StatusColor: "#ffc107",
		HeaderBg:    "#fff3cd",
		Message:     message,
	}
	if isBlocked {
		data.StatusEmoji = "🚫"
		data.StatusText = "Access Blocked"
		data.StatusColor = "#dc3545"
		data.HeaderBg = "#f8d7da"
	}

	barColor := func(pct float64) string {
		if pct >= 100 {
			return "#dc3545"
		} else if pct >= 90 {
			return "#fd7e14"
		} else if pct >= 80 {
			return "#ffc107"
		}
		return "#28a745"
	}

	clamp := func(v float64) float64 {
		if v > 100 {
			return 100
		}
		return v
	}

	percent := func(v float64) string { return fmt.Sprintf("%.0f", v) }

	data.MonthlyTokens = humanizeNumber(int64(monthlyTokens))
	data.MonthlyLimit = humanizeNumber(int64(monthlyLimit))
	data.MonthlyPercent = percent(monthlyPercent)
	data.MonthlyWidth = percent(clamp(monthlyPercent))
	data.MonthlyBarColor = barColor(monthlyPercent)

	if dailyLimit > 0 {
		data.ShowDaily = true
		data.DailyTokens = humanizeNumber(int64(dailyTokens))
		data.DailyLimit = humanizeNumber(int64(dailyLimit))
		data.DailyPercent = percent(dailyPercent)
		data.DailyWidth = percent(clamp(dailyPercent))
		data.DailyBarColor = barColor(dailyPercent)
	}

	return data
}
