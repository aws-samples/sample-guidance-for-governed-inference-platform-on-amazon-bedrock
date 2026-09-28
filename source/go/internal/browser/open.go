package browser

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"

	"gip-go/internal/proc"
)

var errNoBrowser = errors.New("browser: no supported browser launcher found")

type command struct {
	name string
	args []string
}

// OpenURL opens a URL in the user's default browser.
// Respects $BROWSER env var, which is critical for WSL environments where
// xdg-open opens a Linux browser instead of the Windows host browser.
func OpenURL(url string) error {
	if b := os.Getenv("BROWSER"); b != "" {
		// Honoring $BROWSER is the documented escape hatch (critical on WSL).
		// The command comes from the invoking user's own environment and runs
		// as that same user — no privilege boundary is crossed — and url is
		// always program-constructed (localhost callback / IdP authorize URL),
		// passed as a single argv element with no shell interpretation.
		return launch(b, nil, url)
	}

	for _, candidate := range defaultCommands(runtime.GOOS) {
		err := launch(candidate.name, candidate.args, url)
		if errors.Is(err, exec.ErrNotFound) {
			continue
		}
		return err
	}
	return errNoBrowser
}

func launch(name string, prefixArgs []string, url string) error {
	lp, err := exec.LookPath(name)
	if err != nil {
		// A missing or cwd-relative launcher fails cleanly rather than letting
		// process creation perform another search (CWE-427).
		return fmt.Errorf("browser: resolve launcher: %w", err)
	}
	// LookPath can return an explicit relative $BROWSER (for example
	// ./open-host); proc accepts only absolute executable paths.
	abs, err := filepath.Abs(lp)
	if err != nil {
		return fmt.Errorf("browser: resolve launcher path: %w", err)
	}
	argv := make([]string, 0, len(prefixArgs)+2)
	argv = append(argv, abs)
	argv = append(argv, prefixArgs...)
	argv = append(argv, url)
	if err := proc.StartDetached(abs, argv); err != nil {
		return fmt.Errorf("browser: start launcher: %w", err)
	}
	return nil
}

func defaultCommands(goos string) []command {
	switch goos {
	case "darwin":
		return []command{{name: "/usr/bin/open"}}
	case "windows":
		systemRoot := strings.TrimRight(os.Getenv("SystemRoot"), `\/`)
		if systemRoot == "" {
			return nil
		}
		return []command{{name: systemRoot + `\System32\rundll32.exe`, args: []string{"url.dll,FileProtocolHandler"}}}
	case "linux":
		return []command{{name: "xdg-open"}, {name: "x-www-browser"}, {name: "www-browser"}}
	default:
		return nil
	}
}
