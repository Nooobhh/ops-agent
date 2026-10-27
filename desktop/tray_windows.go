package main

import (
	"fmt"
	"math"
	"time"
	"unsafe"

	"github.com/Nooobhh/ops-agent/desktop/internal/apphost"
)

const trayID = 1

var stateColors = map[apphost.State][3]uint8{
	apphost.StateStopped:      {138, 138, 138},
	apphost.StateStarting:     {224, 161, 0},
	apphost.StateRunning:      {46, 158, 68},
	apphost.StateUnresponsive: {224, 161, 0},
	apphost.StateStopping:     {224, 161, 0},
	apphost.StateFailed:       {208, 59, 59},
}

type iconKey struct {
	state apphost.State
	badge bool
}

type tray struct {
	hwnd  uintptr
	icons map[iconKey]uintptr
}

func newTray(hwnd uintptr) *tray {
	t := &tray{hwnd: hwnd, icons: map[iconKey]uintptr{}}
	for state, color := range stateColors {
		t.icons[iconKey{state, false}] = dotIcon(color, false)
		t.icons[iconKey{state, true}] = dotIcon(color, true)
	}
	return t
}

func (t *tray) base() notifyIconData {
	d := notifyIconData{HWnd: t.hwnd, UID: trayID}
	d.CbSize = uint32(unsafe.Sizeof(d))
	return d
}

// add 在登录自启时 explorer 可能还没就绪,失败重试。
func (t *tray) add(state apphost.State, badge bool, tip string) error {
	d := t.base()
	d.UFlags = nifMessage | nifIcon | nifTip | nifShowTip
	d.UCallbackMessage = wmTray
	d.HIcon = t.icons[iconKey{state, badge}]
	copyUTF16(d.SzTip[:], tip)
	var err error
	for i := 0; i < 10; i++ {
		r, _, e := procShellNotifyIconW.Call(nimAdd, uintptr(unsafe.Pointer(&d)))
		if r != 0 {
			d.UVersion = notifyIconV4
			procShellNotifyIconW.Call(nimSetVersion, uintptr(unsafe.Pointer(&d)))
			return nil
		}
		err = e
		time.Sleep(time.Second)
	}
	return fmt.Errorf("添加托盘图标失败: %v", err)
}

// update 可在任意线程调用:Shell_NotifyIcon 只是把数据交给 explorer。
func (t *tray) update(state apphost.State, badge bool, tip string) {
	d := t.base()
	d.UFlags = nifIcon | nifTip | nifShowTip
	d.HIcon = t.icons[iconKey{state, badge}]
	copyUTF16(d.SzTip[:], tip)
	procShellNotifyIconW.Call(nimModify, uintptr(unsafe.Pointer(&d)))
}

func (t *tray) remove() {
	d := t.base()
	procShellNotifyIconW.Call(nimDelete, uintptr(unsafe.Pointer(&d)))
}

// anchor 取托盘图标中心与是否在溢出区:溢出区未展开时 GetRect 返回 S_FALSE,
// 展开时返回弹出层里的位置(不在任务栏窗口内);取不到时退回工作区右下角。
func (t *tray) anchor() (int32, int32, bool) {
	id := notifyIconIdentifier{HWnd: t.hwnd, UID: trayID}
	id.CbSize = uint32(unsafe.Sizeof(id))
	var r rect
	hr, _, _ := procShellNotifyIconGetRect.Call(uintptr(unsafe.Pointer(&id)), uintptr(unsafe.Pointer(&r)))
	if hr == 0 && r.Right > r.Left {
		var bar rect
		taskbar, _, _ := procFindWindowW.Call(uintptr(unsafe.Pointer(utf16("Shell_TrayWnd"))), 0)
		procGetWindowRect.Call(taskbar, uintptr(unsafe.Pointer(&bar)))
		cx, cy := (r.Left+r.Right)/2, (r.Top+r.Bottom)/2
		inBar := cx >= bar.Left && cx < bar.Right && cy >= bar.Top && cy < bar.Bottom
		return cx, cy, taskbar != 0 && !inBar
	}
	work := monitorWorkArea(0, 0)
	return work.Right - 1, work.Bottom, hr == sFalse
}

type menuItem struct {
	id      uintptr
	text    string
	enabled bool
}

// popup 必须在 UI 线程调用;返回选中的 id,取消为 0。
func (t *tray) popup(x, y int32, items []menuItem) uintptr {
	menu, _, _ := procCreatePopupMenu.Call()
	defer procDestroyMenu.Call(menu)
	for _, item := range items {
		if item.text == "" {
			procAppendMenuW.Call(menu, mfSeparator, 0, 0)
			continue
		}
		flags := uintptr(mfString)
		if !item.enabled {
			flags |= mfGrayed
		}
		procAppendMenuW.Call(menu, flags, item.id, uintptr(unsafe.Pointer(utf16(item.text))))
	}
	procSetForegroundWindow.Call(t.hwnd)
	cmd, _, _ := procTrackPopupMenu.Call(menu, tpmReturnCmd|tpmRightButton|tpmNoNotify,
		uintptr(x), uintptr(y), 0, t.hwnd, 0)
	procPostMessageW.Call(t.hwnd, wmNull, 0, 0) // 官方要求:否则菜单第二次弹出后点空白不消失
	return cmd
}

// dotIcon 画状态圆点(白描边、边缘抗锯齿);badge 在右上角加蓝色小点表示有更新。
func dotIcon(c [3]uint8, badge bool) uintptr {
	size, _, _ := procGetSystemMetrics.Call(smCxSmIcon)
	n := int(size)
	if n <= 0 {
		n = 16
	}
	bi := bitmapInfoHeader{BiWidth: int32(n), BiHeight: -int32(n), BiPlanes: 1, BiBitCount: 32}
	bi.BiSize = uint32(unsafe.Sizeof(bi))
	hdc, _, _ := procGetDC.Call(0)
	var bits unsafe.Pointer
	color, _, _ := procCreateDIBSection.Call(hdc, uintptr(unsafe.Pointer(&bi)), 0, uintptr(unsafe.Pointer(&bits)), 0, 0)
	procReleaseDC.Call(0, hdc)
	if color == 0 || bits == nil {
		return 0
	}
	pixels := unsafe.Slice((*uint32)(bits), n*n)
	center := float64(n) / 2
	radius := center - 0.5
	ring := math.Max(1, float64(n)/10)
	badgeR := float64(n) * 0.22
	badgeC := float64(n) - badgeR - 0.5
	for y := 0; y < n; y++ {
		for x := 0; x < n; x++ {
			px, py := float64(x)+0.5, float64(y)+0.5
			d := math.Hypot(px-center, py-center)
			alpha := math.Min(1, math.Max(0, radius-d+0.5))
			r, g, b := uint32(c[0]), uint32(c[1]), uint32(c[2])
			if d > radius-ring {
				r, g, b = 255, 255, 255
			}
			if badge {
				if bd := math.Hypot(px-badgeC, py-(float64(n)-badgeC)); bd < badgeR+ring {
					alpha = math.Max(alpha, math.Min(1, badgeR+ring-bd+0.5))
					r, g, b = 255, 255, 255
					if bd < badgeR {
						r, g, b = 15, 108, 189
					}
				}
			}
			pixels[y*n+x] = uint32(alpha*255)<<24 | r<<16 | g<<8 | b
		}
	}
	maskBits := make([]byte, ((n+15)/16)*2*n) // 全 0 掩码:透明度完全由 alpha 通道决定
	mask, _, _ := procCreateBitmap.Call(uintptr(n), uintptr(n), 1, 1, uintptr(unsafe.Pointer(&maskBits[0])))
	info := iconInfo{FIcon: 1, HbmMask: mask, HbmColor: color}
	icon, _, _ := procCreateIconIndirect.Call(uintptr(unsafe.Pointer(&info)))
	procDeleteObject.Call(color)
	procDeleteObject.Call(mask)
	return icon
}
