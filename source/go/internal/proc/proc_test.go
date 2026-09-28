package proc

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

// TestMain doubles as the helper process: when GIP_PROC_TEST_HELPER is set the
// test binary behaves as the child under test instead of running the suite, so
// the tests need no /bin/sh or other platform-specific executable.
func TestMain(m *testing.M) {
	switch os.Getenv("GIP_PROC_TEST_HELPER") {
	case "":
		os.Exit(m.Run())
	case "echo":
		// argv[1:] joined with spaces, no trailing newline.
		fmt.Print(strings.Join(os.Args[1:], " "))
		os.Exit(0)
	case "fail":
		fmt.Print("partial output")
		os.Exit(3)
	case "stderr-fail":
		fmt.Fprint(os.Stderr, "sensitive child diagnostic")
		os.Exit(4)
	case "sleep":
		time.Sleep(time.Minute)
		os.Exit(0)
	case "pid":
		_ = os.WriteFile(os.Args[1], []byte(fmt.Sprint(os.Getpid())), 0o600)
		os.Exit(0)
	case "pid-sleep":
		_ = os.WriteFile(os.Args[1], []byte(fmt.Sprint(os.Getpid())), 0o600)
		time.Sleep(time.Minute)
		os.Exit(0)
	case "touch":
		// argv[1] is a file path to create, proving the detached process ran
		// with the argv it was given.
		_ = os.WriteFile(os.Args[1], []byte("ran"), 0o600)
		os.Exit(0)
	default:
		fmt.Fprintln(os.Stderr, "unknown helper mode")
		os.Exit(2)
	}
}

func helperExe(t *testing.T, mode string) string {
	t.Helper()
	exe, err := os.Executable()
	if err != nil {
		t.Fatalf("os.Executable: %v", err)
	}
	if !filepath.IsAbs(exe) {
		t.Fatalf("os.Executable returned a relative path: %q", exe)
	}
	t.Setenv("GIP_PROC_TEST_HELPER", mode)
	return exe
}

func TestStartDetachedRejectsRelativePath(t *testing.T) {
	err := StartDetached("credential-process", []string{"credential-process"})
	if !errors.Is(err, ErrNotAbsolute) {
		t.Fatalf("StartDetached(relative) error = %v, want ErrNotAbsolute", err)
	}
	err = StartDetached(filepath.Join("bin", "browser"), []string{"browser", "http://127.0.0.1/"})
	if !errors.Is(err, ErrNotAbsolute) {
		t.Fatalf("StartDetached(relative dir) error = %v, want ErrNotAbsolute", err)
	}
}

func TestOutputWithContextRejectsRelativePath(t *testing.T) {
	out, err := OutputWithContext(context.Background(), "credential-process", []string{"credential-process"})
	if !errors.Is(err, ErrNotAbsolute) {
		t.Fatalf("OutputWithContext(relative) error = %v, want ErrNotAbsolute", err)
	}
	if out != nil {
		t.Fatalf("OutputWithContext(relative) out = %q, want nil", out)
	}
}

func TestStartDetachedRunsProcessWithArgv(t *testing.T) {
	exe := helperExe(t, "touch")
	marker := filepath.Join(t.TempDir(), "ran.txt")

	if err := StartDetached(exe, []string{exe, marker}); err != nil {
		t.Fatalf("StartDetached: %v", err)
	}

	deadline := time.Now().Add(10 * time.Second)
	for {
		if data, err := os.ReadFile(marker); err == nil {
			if string(data) != "ran" {
				t.Fatalf("marker content = %q, want %q", data, "ran")
			}
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("detached process did not create %s within 10s", marker)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func TestStartDetachedDoesNotWaitForProcessExit(t *testing.T) {
	exe := helperExe(t, "pid-sleep")
	pidFile := filepath.Join(t.TempDir(), "pid")

	start := time.Now()
	if err := StartDetached(exe, []string{exe, pidFile}); err != nil {
		t.Fatalf("StartDetached: %v", err)
	}
	if elapsed := time.Since(start); elapsed > 2*time.Second {
		t.Fatalf("StartDetached blocked for %v", elapsed)
	}

	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		data, err := os.ReadFile(pidFile)
		if err == nil {
			pid, err := strconv.Atoi(string(data))
			if err != nil {
				t.Fatalf("parse helper pid %q: %v", data, err)
			}
			process, err := os.FindProcess(pid)
			if err != nil {
				t.Fatalf("find helper process %d: %v", pid, err)
			}
			if err := process.Kill(); err != nil {
				t.Fatalf("kill helper process %d: %v", pid, err)
			}
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("detached helper did not publish its pid")
}

func TestOutputWithContextCapturesStdout(t *testing.T) {
	exe := helperExe(t, "echo")

	out, err := OutputWithContext(context.Background(), exe, []string{exe, "--profile", "team a", "--get-monitoring-token"})
	if err != nil {
		t.Fatalf("OutputWithContext: %v", err)
	}
	// "team a" must survive as one argv element: no shell splitting.
	if got, want := string(out), "--profile team a --get-monitoring-token"; got != want {
		t.Fatalf("stdout = %q, want %q", got, want)
	}
}

func TestOutputWithContextReportsNonZeroExit(t *testing.T) {
	exe := helperExe(t, "fail")

	out, err := OutputWithContext(context.Background(), exe, []string{exe})
	if err == nil {
		t.Fatal("OutputWithContext: expected an error for exit status 3, got nil")
	}
	if !strings.Contains(err.Error(), "exit status 3") {
		t.Fatalf("error = %v, want it to carry the exit status", err)
	}
	if string(out) != "partial output" {
		t.Fatalf("stdout = %q, want the output written before the failing exit", out)
	}
}

func TestOutputWithContextDoesNotReturnChildStderr(t *testing.T) {
	exe := helperExe(t, "stderr-fail")

	out, err := OutputWithContext(context.Background(), exe, []string{exe})
	if err == nil {
		t.Fatal("OutputWithContext: expected an error for exit status 4, got nil")
	}
	const sensitive = "sensitive child diagnostic"
	if strings.Contains(string(out), sensitive) || strings.Contains(err.Error(), sensitive) {
		t.Fatal("OutputWithContext exposed child stderr")
	}
}

func TestOutputWithContextKillsOnDeadline(t *testing.T) {
	exe := helperExe(t, "sleep")
	ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
	defer cancel()

	start := time.Now()
	_, err := OutputWithContext(ctx, exe, []string{exe})
	elapsed := time.Since(start)

	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("error = %v, want context.DeadlineExceeded", err)
	}
	// The helper sleeps for a minute; returning promptly proves it was killed.
	if elapsed > 10*time.Second {
		t.Fatalf("OutputWithContext took %v after the deadline, process was not killed", elapsed)
	}
}
