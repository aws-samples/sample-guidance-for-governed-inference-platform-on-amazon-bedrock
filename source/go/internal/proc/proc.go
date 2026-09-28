// Package proc is the single audited process-spawning site for the Go binaries.
//
// It is built on os.StartProcess rather than os/exec on purpose: os.StartProcess
// performs no PATH lookup, no shell interpretation and no resolution relative to
// the working directory, so the executable that runs is exactly the file named
// by the caller. Callers resolve the executable themselves (exec.LookPath for a
// user-configured $BROWSER, the fixed install location for sibling binaries)
// and must pass an absolute path; anything else is rejected before a process is
// created (CWE-427, untrusted search path).
//
// argv follows the os.StartProcess convention: argv[0] is the program name as
// seen by the child, and every element is a discrete argument.
package proc

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
)

// ErrNotAbsolute is returned when the executable path is not absolute.
var ErrNotAbsolute = errors.New("proc: executable path must be absolute")

// StartDetached starts absPath with argv and returns without blocking on it.
// stdin, stdout and stderr are attached to os.DevNull. A waiter goroutine reaps
// the child on Unix and releases its process resources on Windows.
//
// Browser launchers normally exit as soon as they hand the URL to the desktop.
// A deliberately long-lived launcher keeps one waiter goroutine for that
// process's lifetime; this is the portable trade-off for reaping without
// blocking the caller.
func StartDetached(absPath string, argv []string) error {
	if !filepath.IsAbs(absPath) {
		return fmt.Errorf("%w: %q", ErrNotAbsolute, absPath)
	}
	devNull, err := os.OpenFile(os.DevNull, os.O_RDWR, 0)
	if err != nil {
		return err
	}
	defer devNull.Close()

	p, err := os.StartProcess(absPath, argv, &os.ProcAttr{
		Files: []*os.File{devNull, devNull, devNull},
	})
	if err != nil {
		return err
	}
	go func() {
		_, _ = p.Wait()
	}()
	return nil
}

// OutputWithContext runs absPath with argv and returns what it wrote to stdout.
// stdin and stderr are attached to os.DevNull. stderr is deliberately not
// returned: this helper invokes credential-producing children, whose debug
// output can contain tokens or identity data. The exit status remains available
// as a safe diagnostic. If ctx is done before the process exits, the process is
// killed and ctx.Err() is returned. A non-zero exit status is returned as an
// error together with the captured stdout.
func OutputWithContext(ctx context.Context, absPath string, argv []string) ([]byte, error) {
	if !filepath.IsAbs(absPath) {
		return nil, fmt.Errorf("%w: %q", ErrNotAbsolute, absPath)
	}
	devNull, err := os.OpenFile(os.DevNull, os.O_RDWR, 0)
	if err != nil {
		return nil, err
	}
	defer devNull.Close()

	pr, pw, err := os.Pipe()
	if err != nil {
		return nil, err
	}
	p, err := os.StartProcess(absPath, argv, &os.ProcAttr{
		Files: []*os.File{devNull, pw, devNull},
	})
	// The child holds its own copy of the write end; closing ours is what lets
	// the read end see EOF when the child exits.
	pw.Close()
	if err != nil {
		pr.Close()
		return nil, err
	}

	// Kill the child if ctx is done first. The result channel reports whether
	// the kill actually interrupted a running process: if the child had already
	// exited, its output is complete and ctx is not reported as the failure.
	finished := make(chan struct{})
	ctxErr := make(chan error, 1)
	go func() {
		select {
		case <-ctx.Done():
			if killErr := p.Kill(); killErr == nil {
				ctxErr <- ctx.Err()
				return
			}
			ctxErr <- nil
		case <-finished:
			ctxErr <- nil
		}
	}()

	out, readErr := io.ReadAll(pr)
	pr.Close()
	state, waitErr := p.Wait()
	close(finished)

	if cerr := <-ctxErr; cerr != nil {
		return out, cerr
	}
	if waitErr != nil {
		return out, waitErr
	}
	if !state.Success() {
		return out, fmt.Errorf("%s: %v", filepath.Base(absPath), state)
	}
	return out, readErr
}
