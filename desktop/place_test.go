package main

import "testing"

func TestPlacePanelHugsTaskbar(t *testing.T) {
	work := box{0, 0, 1920, 1040} // 底部任务栏占 40px
	cases := []struct {
		name   string
		ax, ay int32
		x, y   int32
	}{
		{"底部任务栏", 1800, 1060, 1920 - 300 - 8, 1040 - 400 - 8},
		{"顶部任务栏", 1800, -20, 1920 - 300 - 8, 8},
		{"右侧任务栏", 1940, 600, 1920 - 300 - 8, 400},
		{"左侧任务栏", -20, 600, 8, 400},
		{"溢出区展开时锚点在工作区内", 960, 900, 810, 900 - 400 - 8},
		{"贴边夹回工作区", 1919, 1060, 1920 - 300 - 8, 1040 - 400 - 8},
	}
	for _, c := range cases {
		x, y := placePanel(c.ax, c.ay, 300, 400, 8, work)
		if x != c.x || y != c.y {
			t.Errorf("%s: (%d,%d) 期望 (%d,%d)", c.name, x, y, c.x, c.y)
		}
	}
}

func TestScaleByDPI(t *testing.T) {
	if scale(300, 96) != 300 || scale(300, 120) != 375 || scale(300, 144) != 450 {
		t.Fatal("DPI 缩放不对")
	}
}
