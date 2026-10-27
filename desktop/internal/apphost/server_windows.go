package apphost

import (
	"os/exec"
	"strconv"
	"syscall"
)

const createNoWindow = 0x08000000

// hideConsole 用 python.exe + CREATE_NO_WINDOW:worker 子进程继承隐藏控制台,不弹黑窗。
func hideConsole(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: createNoWindow}
}

// killTree 强杀单个进程会在 Windows 上留下孤儿 triage worker,必须带 /T。
func killTree(pid int) {
	kill := exec.Command("taskkill", "/PID", strconv.Itoa(pid), "/T", "/F")
	hideConsole(kill)
	_ = kill.Run()
}
