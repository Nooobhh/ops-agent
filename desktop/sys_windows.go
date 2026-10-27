package main

import (
	"errors"
	"fmt"
	"sync"
	"unsafe"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/registry"
)

const (
	mutexName     = `Local\OpsAgentShell`
	handoverEvent = `Local\OpsAgentShellHandover`
	runKey        = `Software\Microsoft\Windows\CurrentVersion\Run`
	runValue      = "OpsAgent"
)

// ---- 防睡眠:中台运行期间持有 PowerRequestSystemRequired,`powercfg /requests` 可见 ----

type keepAwake struct {
	mu     sync.Mutex
	handle windows.Handle
	held   bool
}

func (k *keepAwake) set(hold bool) {
	k.mu.Lock()
	defer k.mu.Unlock()
	if hold == k.held {
		return
	}
	if k.handle == 0 {
		ctx := reasonContext{Flags: powerRequestContextSimpleString, Reason: utf16("ops-agent 中台运行中")}
		h, _, err := procPowerCreateRequest.Call(uintptr(unsafe.Pointer(&ctx)))
		if windows.Handle(h) == windows.InvalidHandle || h == 0 {
			logf("创建电源请求失败: %v", err)
			return
		}
		k.handle = windows.Handle(h)
	}
	proc := procPowerClearRequest
	if hold {
		proc = procPowerSetRequest
	}
	if r, _, err := proc.Call(uintptr(k.handle), powerRequestSystemRequired); r == 0 {
		logf("电源请求切换失败: %v", err)
		return
	}
	k.held = hold
	logf("防睡眠 %v", hold)
}

// ---- 登录自启:HKCU Run,不要管理员权限 ----

func autostartCommand(exe string) string { return fmt.Sprintf(`"%s" --no-browser`, exe) }

func autostartEnabled(exe string) bool {
	k, err := registry.OpenKey(registry.CURRENT_USER, runKey, registry.QUERY_VALUE)
	if err != nil {
		return false
	}
	defer k.Close()
	v, _, err := k.GetStringValue(runValue)
	return err == nil && v == autostartCommand(exe)
}

func setAutostart(exe string, on bool) error {
	k, _, err := registry.CreateKey(registry.CURRENT_USER, runKey, registry.SET_VALUE)
	if err != nil {
		return err
	}
	defer k.Close()
	if on {
		return k.SetStringValue(runValue, autostartCommand(exe))
	}
	if err := k.DeleteValue(runValue); err != nil && !errors.Is(err, registry.ErrNotExist) {
		return err
	}
	return nil
}

// ---- 单实例与外壳交接 ----

// acquireMutex 已有外壳在跑时返回 false;交接模式下等旧外壳退出释放(最多 waitMs 毫秒)。
func acquireMutex(waitMs int) (windows.Handle, bool) {
	for waited := 0; ; waited += 200 {
		h, err := windows.CreateMutex(nil, false, utf16(mutexName))
		if !errors.Is(err, windows.ERROR_ALREADY_EXISTS) {
			return h, true
		}
		windows.CloseHandle(h) // 不关的话本进程的句柄会让互斥量一直「存在」
		if waited >= waitMs {
			return 0, false
		}
		windows.SleepEx(200, false)
	}
}

// requestQuit 让已运行的外壳停中台并退出(给脚本与冒烟测试用);等它释放互斥量,最多 waitMs 毫秒。
func requestQuit(waitMs int) bool {
	hwnd, _, _ := procFindWindowW.Call(uintptr(unsafe.Pointer(utf16(msgWindowClass))), 0)
	if hwnd == 0 {
		return true
	}
	procPostMessageW.Call(hwnd, wmQuitRequest, 0, 0)
	h, ok := acquireMutex(waitMs)
	if ok {
		closeHandle(h)
	}
	return ok
}

// notifyRunning 让已运行的外壳弹出面板;双击拉起的新进程持有前台权限,转给它面板才拿得到焦点。
func notifyRunning() uintptr {
	hwnd, _, _ := procFindWindowW.Call(uintptr(unsafe.Pointer(utf16(msgWindowClass))), 0)
	if hwnd != 0 {
		procAllowSetForegroundWindow.Call(asfwAny)
		procPostMessageW.Call(hwnd, wmShowPanel, 0, 0)
	}
	return hwnd
}

func createHandoverEvent() (windows.Handle, error) {
	return windows.CreateEvent(nil, 1, 0, utf16(handoverEvent))
}

// signalHandover 新外壳一启动就报到,旧外壳据此确认交接可行后才退出。
func signalHandover() {
	h, err := windows.OpenEvent(windows.EVENT_MODIFY_STATE, false, utf16(handoverEvent))
	if err != nil {
		logf("交接事件不存在: %v", err)
		return
	}
	windows.SetEvent(h)
	windows.CloseHandle(h)
}

func waitEvent(h windows.Handle, ms uint32) bool {
	r, _ := windows.WaitForSingleObject(h, ms)
	return r == windows.WAIT_OBJECT_0
}

func closeHandle(h windows.Handle) { _ = windows.CloseHandle(h) }
