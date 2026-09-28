//go:build darwin || linux

package proc

import (
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"syscall"
	"testing"
	"time"
)

func TestStartDetachedReapsExitedProcess(t *testing.T) {
	exe := helperExe(t, "pid")
	pidFile := filepath.Join(t.TempDir(), "pid")

	if err := StartDetached(exe, []string{exe, pidFile}); err != nil {
		t.Fatalf("StartDetached: %v", err)
	}

	deadline := time.Now().Add(10 * time.Second)
	var pid int
	for time.Now().Before(deadline) {
		data, err := os.ReadFile(pidFile)
		if err == nil {
			pid, err = strconv.Atoi(string(data))
			if err != nil {
				t.Fatalf("parse helper pid %q: %v", data, err)
			}
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if pid == 0 {
		t.Fatal("detached helper did not publish its pid")
	}

	for time.Now().Before(deadline) {
		err := syscall.Kill(pid, 0)
		if errors.Is(err, syscall.ESRCH) {
			return
		}
		if err != nil {
			t.Fatalf("probe helper pid %d: %v", pid, err)
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("detached helper pid %d still exists after exit; child was not reaped", pid)
}
