package main

// box 屏幕矩形(像素),与 Win32 RECT 同义;放在无 build tag 的文件里,mac 上可测。
type box struct{ Left, Top, Right, Bottom int32 }

const panelGap = 8 // 面板与任务栏/屏幕边缘的间距(DIP)

// scale 把 DIP 按显示器 DPI 换成像素。
func scale(v int32, dpi uint32) int32 { return int32(int64(v) * int64(dpi) / 96) }

// placePanel 按锚点(托盘图标中心)落在工作区哪一侧判断任务栏位置,面板贴任务栏弹出并夹在工作区内。
func placePanel(ax, ay, w, h, gap int32, work box) (int32, int32) {
	x, y := ax-w/2, ay-h-gap
	switch {
	case ay >= work.Bottom:
		y = work.Bottom - h - gap
	case ay < work.Top:
		y = work.Top + gap
	case ax >= work.Right:
		x, y = work.Right-w-gap, ay-h/2
	case ax < work.Left:
		x, y = work.Left+gap, ay-h/2
	}
	x = clamp(x, work.Left+gap, work.Right-w-gap)
	y = clamp(y, work.Top+gap, work.Bottom-h-gap)
	return x, y
}

func clamp(v, lo, hi int32) int32 {
	if v > hi {
		v = hi
	}
	if v < lo {
		v = lo
	}
	return v
}
