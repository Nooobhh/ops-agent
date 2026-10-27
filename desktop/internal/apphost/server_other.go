//go:build !windows

package apphost

import (
	"os/exec"
	"syscall"
)

// 非 Windows 只用于 mac 上跑单测:子进程自成进程组,强杀时连同其子进程一起带走。
func hideConsole(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
}

func killTree(pid int) {
	_ = syscall.Kill(-pid, syscall.SIGKILL)
	_ = syscall.Kill(pid, syscall.SIGKILL)
}
