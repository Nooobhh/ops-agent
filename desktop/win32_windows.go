package main

import (
	"unsafe"

	"golang.org/x/sys/windows"
)

var (
	user32   = windows.NewLazySystemDLL("user32.dll")
	shell32  = windows.NewLazySystemDLL("shell32.dll")
	gdi32    = windows.NewLazySystemDLL("gdi32.dll")
	kernel32 = windows.NewLazySystemDLL("kernel32.dll")
	shcore   = windows.NewLazySystemDLL("shcore.dll")

	procShellNotifyIconW              = shell32.NewProc("Shell_NotifyIconW")
	procShellNotifyIconGetRect        = shell32.NewProc("Shell_NotifyIconGetRect")
	procRegisterClassExW              = user32.NewProc("RegisterClassExW")
	procCreateWindowExW               = user32.NewProc("CreateWindowExW")
	procDestroyWindow                 = user32.NewProc("DestroyWindow")
	procDefWindowProcW                = user32.NewProc("DefWindowProcW")
	procGetMessageW                   = user32.NewProc("GetMessageW")
	procTranslateMessage              = user32.NewProc("TranslateMessage")
	procDispatchMessageW              = user32.NewProc("DispatchMessageW")
	procPostQuitMessage               = user32.NewProc("PostQuitMessage")
	procSetTimer                      = user32.NewProc("SetTimer")
	procKillTimer                     = user32.NewProc("KillTimer")
	procSetWindowPos                  = user32.NewProc("SetWindowPos")
	procGetWindowRect                 = user32.NewProc("GetWindowRect")
	procShowWindow                    = user32.NewProc("ShowWindow")
	procAllowSetForegroundWindow      = user32.NewProc("AllowSetForegroundWindow")
	procIsWindowVisible               = user32.NewProc("IsWindowVisible")
	procSetForegroundWindow           = user32.NewProc("SetForegroundWindow")
	procCreatePopupMenu               = user32.NewProc("CreatePopupMenu")
	procAppendMenuW                   = user32.NewProc("AppendMenuW")
	procTrackPopupMenu                = user32.NewProc("TrackPopupMenu")
	procDestroyMenu                   = user32.NewProc("DestroyMenu")
	procPostMessageW                  = user32.NewProc("PostMessageW")
	procFindWindowW                   = user32.NewProc("FindWindowW")
	procRegisterWindowMessageW        = user32.NewProc("RegisterWindowMessageW")
	procMonitorFromPoint              = user32.NewProc("MonitorFromPoint")
	procGetMonitorInfoW               = user32.NewProc("GetMonitorInfoW")
	procGetSystemMetrics              = user32.NewProc("GetSystemMetrics")
	procCreateIconIndirect            = user32.NewProc("CreateIconIndirect")
	procGetDC                         = user32.NewProc("GetDC")
	procReleaseDC                     = user32.NewProc("ReleaseDC")
	procSetProcessDpiAwarenessContext = user32.NewProc("SetProcessDpiAwarenessContext")
	procGetDpiForMonitor              = shcore.NewProc("GetDpiForMonitor")
	procCreateDIBSection              = gdi32.NewProc("CreateDIBSection")
	procCreateBitmap                  = gdi32.NewProc("CreateBitmap")
	procDeleteObject                  = gdi32.NewProc("DeleteObject")
	procPowerCreateRequest            = kernel32.NewProc("PowerCreateRequest")
	procPowerSetRequest               = kernel32.NewProc("PowerSetRequest")
	procPowerClearRequest             = kernel32.NewProc("PowerClearRequest")
)

const (
	wmNull        = 0x0000
	wmDestroy     = 0x0002
	wmSize        = 0x0005
	wmActivate    = 0x0006
	wmClose       = 0x0010
	wmTimer       = 0x0113
	wmContextMenu = 0x007B
	wmDpiChanged  = 0x02E0
	wmApp         = 0x8000
	wmTray        = wmApp + 1 // 托盘回调
	wmShowPanel   = wmApp + 2 // 再次启动 exe 时由新进程投递
	wmDispatch    = wmApp + 3 // 后台 goroutine 投递到 UI 线程的闭包
	wmQuitRequest = wmApp + 4 // OpsAgent.exe --quit:脚本化停中台并退出外壳
	waInactive    = 0

	ninSelect    = 0x0400
	ninKeySelect = 0x0401

	nimAdd        = 0
	nimModify     = 1
	nimDelete     = 2
	nimSetVersion = 4
	nifMessage    = 0x01
	nifIcon       = 0x02
	nifTip        = 0x04
	nifShowTip    = 0x80
	notifyIconV4  = 4

	wsPopup        = 0x80000000
	wsClipChildren = 0x02000000
	wsExToolWindow = 0x00000080
	wsExTopmost    = 0x00000008
	swHide         = 0
	swShowNormal   = 1
	swpNoSize      = 0x0001
	swpNoMove      = 0x0002
	swpNoActivate  = 0x0010
	swpShowWindow  = 0x0040

	mfString       = 0x0000
	mfGrayed       = 0x0001
	mfSeparator    = 0x0800
	tpmRightButton = 0x0002
	tpmNoNotify    = 0x0080
	tpmReturnCmd   = 0x0100

	monitorDefaultToNearest = 2
	mdtEffectiveDpi         = 0
	asfwAny                 = 0xFFFFFFFF // ASFW_ANY
	smCxSmIcon              = 49
	colorWindow             = 5
	sFalse                  = 1

	powerRequestContextSimpleString = 0x1
	powerRequestSystemRequired      = 1
)

var (
	hwndTopmost              = ^uintptr(0)     // (HWND)-1
	dpiAwarenessPerMonitorV2 = ^uintptr(4 - 1) // (DPI_AWARENESS_CONTEXT)-4
)

type rect struct{ Left, Top, Right, Bottom int32 }

type point struct{ X, Y int32 }

type msg struct {
	Hwnd    uintptr
	Message uint32
	WParam  uintptr
	LParam  uintptr
	Time    uint32
	Pt      point
	_       uint32 // lPrivate
}

type wndClassEx struct {
	CbSize        uint32
	Style         uint32
	LpfnWndProc   uintptr
	CbClsExtra    int32
	CbWndExtra    int32
	HInstance     windows.Handle
	HIcon         uintptr
	HCursor       uintptr
	HbrBackground uintptr
	LpszMenuName  *uint16
	LpszClassName *uint16
	HIconSm       uintptr
}

type monitorInfo struct {
	CbSize    uint32
	RcMonitor rect
	RcWork    rect
	DwFlags   uint32
}

type notifyIconData struct {
	CbSize           uint32
	HWnd             uintptr
	UID              uint32
	UFlags           uint32
	UCallbackMessage uint32
	HIcon            uintptr
	SzTip            [128]uint16
	DwState          uint32
	DwStateMask      uint32
	SzInfo           [256]uint16
	UVersion         uint32
	SzInfoTitle      [64]uint16
	DwInfoFlags      uint32
	GuidItem         windows.GUID
	HBalloonIcon     uintptr
}

type notifyIconIdentifier struct {
	CbSize   uint32
	HWnd     uintptr
	UID      uint32
	GuidItem windows.GUID
}

type bitmapInfoHeader struct {
	BiSize          uint32
	BiWidth         int32
	BiHeight        int32
	BiPlanes        uint16
	BiBitCount      uint16
	BiCompression   uint32
	BiSizeImage     uint32
	BiXPelsPerMeter int32
	BiYPelsPerMeter int32
	BiClrUsed       uint32
	BiClrImportant  uint32
}

type iconInfo struct {
	FIcon    int32
	XHotspot uint32
	YHotspot uint32
	HbmMask  uintptr
	HbmColor uintptr
}

type reasonContext struct {
	Version uint32
	Flags   uint32
	Reason  *uint16
}

func utf16(s string) *uint16 {
	p, _ := windows.UTF16PtrFromString(s)
	return p
}

func copyUTF16(dst []uint16, s string) {
	src, _ := windows.UTF16FromString(s)
	if len(src) > len(dst) {
		src = append(src[:len(dst)-1], 0)
	}
	copy(dst, src)
}

func setWindowPos(hwnd, after uintptr, x, y, w, h int32, flags uintptr) {
	procSetWindowPos.Call(hwnd, after, uintptr(x), uintptr(y), uintptr(w), uintptr(h), flags)
}

func isWindowVisible(hwnd uintptr) bool {
	r, _, _ := procIsWindowVisible.Call(hwnd)
	return r != 0
}

func pointArg(x, y int32) uintptr {
	return uintptr(uint32(x)) | uintptr(uint32(y))<<32 // POINT 按值传,amd64 上占一个寄存器
}

func monitorAt(x, y int32) uintptr {
	mon, _, _ := procMonitorFromPoint.Call(pointArg(x, y), monitorDefaultToNearest)
	return mon
}

func monitorWorkArea(x, y int32) rect {
	mi := monitorInfo{CbSize: uint32(unsafe.Sizeof(monitorInfo{}))}
	procGetMonitorInfoW.Call(monitorAt(x, y), uintptr(unsafe.Pointer(&mi)))
	return mi.RcWork
}

// monitorDPI 按显示器取有效 DPI(Per-Monitor V2 下面板尺寸按它缩放);取不到按 96。
func monitorDPI(x, y int32) uint32 {
	var dx, dy uint32
	if procGetDpiForMonitor.Find() != nil {
		return 96
	}
	hr, _, _ := procGetDpiForMonitor.Call(monitorAt(x, y), mdtEffectiveDpi,
		uintptr(unsafe.Pointer(&dx)), uintptr(unsafe.Pointer(&dy)))
	if hr != 0 || dx == 0 {
		return 96
	}
	return dx
}

func shellOpen(target string) {
	_ = windows.ShellExecute(0, utf16("open"), utf16(target), nil, nil, swShowNormal)
}

func messageBox(text string) {
	windows.MessageBox(0, utf16(text), utf16(windowTitle), windows.MB_OK|windows.MB_ICONERROR)
}
