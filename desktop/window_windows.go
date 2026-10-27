package main

import (
	"sync"
	"unsafe"

	"golang.org/x/sys/windows"
)

const (
	msgWindowClass = "OpsAgentShellMsg" // 单实例通知按类名找它
	timerPanelIdle = 1
)

// ui 是 UI 线程的隐藏消息窗口:接托盘回调、单实例通知、explorer 重启广播与定时器,
// 并承接后台 goroutine 投递的闭包。不用 HWND_MESSAGE:那种窗口收不到 TaskbarCreated 广播。
type ui struct {
	hwnd           uintptr
	taskbarCreated uintptr

	mu    sync.Mutex
	queue []func()

	onTray           func(event uintptr, x, y int32)
	onShowRequest    func()
	onQuitRequest    func()
	onTaskbarCreated func()
	onTimer          func(id uintptr)
}

var (
	wndProcs   sync.Map // hwnd → func(msg, wp, lp) (uintptr, bool)
	wndProcPtr = windows.NewCallback(dispatchWndProc)
	hInstance  windows.Handle
)

func init() { _ = windows.GetModuleHandleEx(0, nil, &hInstance) }

func dispatchWndProc(hwnd, msg, wp, lp uintptr) uintptr {
	if f, ok := wndProcs.Load(hwnd); ok {
		if r, handled := f.(func(uintptr, uintptr, uintptr) (uintptr, bool))(msg, wp, lp); handled {
			return r
		}
	}
	r, _, _ := procDefWindowProcW.Call(hwnd, msg, wp, lp)
	return r
}

// registerClass 所有窗口共用一个窗口过程,按 hwnd 分派;CreateWindowEx 返回前的消息(WM_CREATE 等)走缺省处理。
func registerClass(name string) error {
	// 白底:WebView2 内容出来前不透出底下的屏幕残影
	wc := wndClassEx{LpfnWndProc: wndProcPtr, HInstance: hInstance, LpszClassName: utf16(name), HbrBackground: colorWindow + 1}
	wc.CbSize = uint32(unsafe.Sizeof(wc))
	if r, _, err := procRegisterClassExW.Call(uintptr(unsafe.Pointer(&wc))); r == 0 && err != windows.ERROR_CLASS_ALREADY_EXISTS {
		return err
	}
	return nil
}

func createWindow(class, title string, style, exStyle uintptr, w, h int32,
	proc func(msg, wp, lp uintptr) (uintptr, bool)) (uintptr, error) {
	hwnd, _, err := procCreateWindowExW.Call(exStyle, uintptr(unsafe.Pointer(utf16(class))),
		uintptr(unsafe.Pointer(utf16(title))), style, 0, 0, uintptr(w), uintptr(h), 0, 0, uintptr(hInstance), 0)
	if hwnd == 0 {
		return 0, err
	}
	wndProcs.Store(hwnd, proc)
	return hwnd, nil
}

func destroyWindow(hwnd uintptr) {
	procDestroyWindow.Call(hwnd)
	wndProcs.Delete(hwnd)
}

func newUI() (*ui, error) {
	u := &ui{}
	if err := registerClass(msgWindowClass); err != nil {
		return nil, err
	}
	hwnd, err := createWindow(msgWindowClass, windowTitle, wsPopup, wsExToolWindow, 0, 0, u.wndProc)
	if err != nil {
		return nil, err
	}
	u.hwnd = hwnd
	// explorer 重启后会广播 TaskbarCreated,须重新添加托盘图标
	u.taskbarCreated, _, _ = procRegisterWindowMessageW.Call(uintptr(unsafe.Pointer(utf16("TaskbarCreated"))))
	return u, nil
}

func (u *ui) wndProc(msg, wp, lp uintptr) (uintptr, bool) {
	switch msg {
	case wmTray:
		// NOTIFYICON_VERSION_4:事件在 lParam 低字,锚点坐标在 wParam
		u.onTray(lp&0xFFFF, int32(int16(wp&0xFFFF)), int32(int16(wp>>16&0xFFFF)))
		return 0, true
	case wmShowPanel:
		u.onShowRequest()
		return 0, true
	case wmQuitRequest:
		u.onQuitRequest()
		return 0, true
	case wmDispatch:
		u.drain()
		return 0, true
	case wmTimer:
		u.onTimer(wp)
		return 0, true
	}
	if u.taskbarCreated != 0 && msg == u.taskbarCreated {
		u.onTaskbarCreated()
		return 0, true
	}
	return 0, false
}

// dispatch 可在任意 goroutine 调用,闭包在 UI 线程执行。
func (u *ui) dispatch(f func()) {
	u.mu.Lock()
	u.queue = append(u.queue, f)
	u.mu.Unlock()
	procPostMessageW.Call(u.hwnd, wmDispatch, 0, 0)
}

func (u *ui) drain() {
	u.mu.Lock()
	q := u.queue
	u.queue = nil
	u.mu.Unlock()
	for _, f := range q {
		f()
	}
}

func (u *ui) setTimer(id uintptr, ms uint32) { procSetTimer.Call(u.hwnd, id, uintptr(ms), 0) }
func (u *ui) killTimer(id uintptr)           { procKillTimer.Call(u.hwnd, id) }

// run 消息循环,PostQuitMessage 后返回。
func (u *ui) run() {
	var m msg
	for {
		r, _, _ := procGetMessageW.Call(uintptr(unsafe.Pointer(&m)), 0, 0, 0)
		if int32(r) <= 0 {
			return
		}
		procTranslateMessage.Call(uintptr(unsafe.Pointer(&m)))
		procDispatchMessageW.Call(uintptr(unsafe.Pointer(&m)))
	}
}

func quitLoop() { procPostQuitMessage.Call(0) }
