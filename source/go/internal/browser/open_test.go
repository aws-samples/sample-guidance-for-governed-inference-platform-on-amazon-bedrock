package browser

import (
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"
)

func TestMain(m *testing.M) {
	if marker := os.Getenv("GIP_BROWSER_TEST_MARKER"); marker != "" {
		_ = os.WriteFile(marker, []byte(strings.Join(os.Args[1:], "\n")), 0o600)
		os.Exit(0)
	}
	os.Exit(m.Run())
}

func TestOpenURLResolvesBrowserAndPassesURLAsOneArgument(t *testing.T) {
	exe, err := os.Executable()
	if err != nil {
		t.Fatalf("os.Executable: %v", err)
	}
	marker := filepath.Join(t.TempDir(), "browser-argv")
	t.Setenv("PATH", filepath.Dir(exe))
	t.Setenv("BROWSER", filepath.Base(exe))
	t.Setenv("GIP_BROWSER_TEST_MARKER", marker)
	const url = "http://localhost:8401/quota-status?value=one%20two&other=three"

	if err := OpenURL(url); err != nil {
		t.Fatalf("OpenURL: %v", err)
	}

	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		data, err := os.ReadFile(marker)
		if err == nil {
			if got := string(data); got != url {
				t.Fatalf("browser argv = %q, want one URL argument %q", got, url)
			}
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("browser helper did not run")
}

func TestOpenURLResolutionErrorDoesNotContainURL(t *testing.T) {
	t.Setenv("BROWSER", "gip-browser-that-does-not-exist")
	const sensitiveURL = "http://localhost/callback?code=sensitive-code"

	err := OpenURL(sensitiveURL)
	if err == nil {
		t.Fatal("OpenURL: expected missing browser error")
	}
	if strings.Contains(err.Error(), sensitiveURL) || strings.Contains(err.Error(), "sensitive-code") {
		t.Fatalf("OpenURL error exposed URL: %v", err)
	}
}

func TestDefaultCommandsCoverSupportedPlatforms(t *testing.T) {
	tests := []struct {
		goos       string
		systemRoot string
		wantNames  []string
		wantPrefix []string
	}{
		{goos: "darwin", wantNames: []string{"/usr/bin/open"}},
		{goos: "linux", wantNames: []string{"xdg-open", "x-www-browser", "www-browser"}},
		{
			goos:       "windows",
			systemRoot: `C:\Windows`,
			wantNames:  []string{`C:\Windows\System32\rundll32.exe`},
			wantPrefix: []string{"url.dll,FileProtocolHandler"},
		},
	}

	for _, tt := range tests {
		t.Run(tt.goos, func(t *testing.T) {
			t.Setenv("SystemRoot", tt.systemRoot)
			commands := defaultCommands(tt.goos)
			if len(commands) != len(tt.wantNames) {
				t.Fatalf("defaultCommands(%q) returned %d commands, want %d", tt.goos, len(commands), len(tt.wantNames))
			}
			for i, command := range commands {
				if command.name != tt.wantNames[i] {
					t.Errorf("command %d name = %q, want %q", i, command.name, tt.wantNames[i])
				}
				if !slices.Equal(command.args, tt.wantPrefix) {
					t.Errorf("command %d args = %q, want %q", i, command.args, tt.wantPrefix)
				}
			}
		})
	}

	if got := defaultCommands("plan9"); got != nil {
		t.Fatalf("defaultCommands(unsupported) = %v, want nil", got)
	}
	t.Setenv("SystemRoot", "")
	if got := defaultCommands("windows"); got != nil {
		t.Fatalf("defaultCommands(windows without SystemRoot) = %v, want nil", got)
	}
}
