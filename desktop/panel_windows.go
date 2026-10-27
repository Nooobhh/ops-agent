package main

import (
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"time"

	"github.com/Nooobhh/ops-agent/desktop/internal/webview2/edge"
)

//go:embed panel.html
var panelHTML string

const (
	panelClass  = "OpsAgentPanel"
	panelWidth  = 320 // DIP
	panelHeight = 420
)

// panelIdle 隐藏这么久后销毁 WebView2,空闲时外壳不背着 6 个浏览器进程(约 120MB 私有内存);验收时用 --panel-idle 调短
var panelIdle = 10 * time.Minute

// bridgeScript 面板页调 window.shell(method, ...params) 得到 Promise;Go 处理后经 __shellReply 回送。
const bridgeScript = `(() => {
  let seq = 0; const pending = {};
  window.shell = (method, ...params) => new Promise((resolve, reject) => {
    const id = ++seq; pending[id] = { resolve, reject };
    window.chrome.webview.postMessage(JSON.stringify({ id, method, params }));
  });
  window.__shellReply = (id, ok, value) => {
    const p = pending[id]; delete pending[id];
    if (p) (ok ? p.resolve : p.reject)(ok ? value : new Error(value));
  };
})();`

type handler func(params []json.RawMessage) (any, error)

// panel 贴托盘弹出的 WebView2 小窗:首次打开才创建,隐藏 panelIdle 后销毁。
type panel struct {
	ui       *ui
	dataPath string
	handlers map[string]handler

	hwnd     uintptr
	chromium *edge.Chromium
	creating bool
	loaded   bool
	showNext bool // 首次加载完成后再显示,避免白屏闪一下
	lastHide time.Time
	width    int32
	height   int32
}

func newPanel(u *ui, dataPath string, handlers map[string]handler) (*panel, error) {
	if err := registerClass(panelClass); err != nil {
		return nil, err
	}
	return &panel{ui: u, dataPath: dataPath, handlers: handlers}, nil
}

func (p *panel) visible() bool { return p.hwnd != 0 && isWindowVisible(p.hwnd) }

func (p *panel) toggle(x, y int32) {
	if p.visible() {
		p.hide()
		return
	}
	// 面板开着时点托盘:按下先触发失焦隐藏,抬起才到这里——不能再弹回来
	if time.Since(p.lastHide) < 300*time.Millisecond {
		return
	}
	p.showAt(x, y)
}

func (p *panel) showAt(x, y int32) {
	if p.creating {
		return // 创建 WebView2 的嵌套消息循环里又来了点击
	}
	p.ui.killTimer(timerPanelIdle)
	dpi := monitorDPI(x, y)
	p.width, p.height = scale(panelWidth, dpi), scale(panelHeight, dpi)
	work := monitorWorkArea(x, y)
	px, py := placePanel(x, y, p.width, p.height, scale(panelGap, dpi), box(work))
	if p.hwnd == 0 {
		if err := p.create(); err != nil {
			log.Printf("面板创建失败: %v", err)
			messageBox(err.Error())
			return
		}
	}
	setWindowPos(p.hwnd, hwndTopmost, px, py, p.width, p.height, swpNoActivate)
	if !p.loaded {
		p.showNext = true
		return
	}
	p.reveal()
}

func (p *panel) reveal() {
	setWindowPos(p.hwnd, hwndTopmost, 0, 0, 0, 0, swpShowWindow|swpNoSize|swpNoMove)
	procSetForegroundWindow.Call(p.hwnd)
	// 控制器在隐藏窗口里创建、隐藏时又被设为不可见:显示后必须重新设可见并同步位置,否则不渲染
	_ = p.chromium.Show()
	p.chromium.Resize()
	_ = p.chromium.NotifyParentWindowPositionChanged()
	p.chromium.Focus()
	p.chromium.Eval("window.refresh && window.refresh()")
}

func (p *panel) create() error {
	started := time.Now()
	p.creating = true
	defer func() { p.creating = false }()
	hwnd, err := createWindow(panelClass, windowTitle, wsPopup|wsClipChildren, wsExToolWindow|wsExTopmost,
		p.width, p.height, p.wndProc)
	if err != nil {
		return fmt.Errorf("创建面板窗口失败: %w", err)
	}
	c := edge.NewChromium()
	c.DataPath = p.dataPath
	c.MessageCallback = p.onMessage
	c.NavigationCompletedCallback = func(*edge.ICoreWebView2, *edge.ICoreWebView2NavigationCompletedEventArgs) {
		p.loaded = true
		log.Printf("面板就绪,耗时 %s", time.Since(started).Round(time.Millisecond))
		if p.showNext {
			p.showNext = false
			p.reveal()
		}
	}
	p.hwnd = hwnd
	if !c.Embed(hwnd) {
		destroyWindow(hwnd)
		p.hwnd = 0
		return errors.New("WebView2 初始化失败,请确认已安装 Microsoft Edge WebView2 Runtime")
	}
	p.chromium = c
	if settings, err := c.GetSettings(); err == nil {
		_ = settings.PutAreDefaultContextMenusEnabled(false)
		_ = settings.PutAreDevToolsEnabled(false)
		_ = settings.PutIsZoomControlEnabled(false)
		_ = settings.PutIsStatusBarEnabled(false)
		_ = settings.PutAreBrowserAcceleratorKeysEnabled(false)
	}
	if c2 := c.GetController().GetICoreWebView2Controller2(); c2 != nil {
		_ = c2.PutDefaultBackgroundColor(edge.COREWEBVIEW2_COLOR{A: 255, R: 255, G: 255, B: 255})
	}
	c.Resize()
	c.Init(bridgeScript)
	c.NavigateToString(panelHTML)
	return nil
}

func (p *panel) hide() {
	if p.hwnd == 0 {
		return
	}
	procShowWindow.Call(p.hwnd, swHide)
	if p.chromium != nil {
		_ = p.chromium.Hide() // 隐藏期间 WebView2 停止渲染
	}
	p.lastHide = time.Now()
	p.ui.setTimer(timerPanelIdle, uint32(panelIdle/time.Millisecond))
}

// destroyIfIdle 定时器到点:面板仍隐藏就关掉 WebView2 并销毁窗口。
func (p *panel) destroyIfIdle() {
	p.ui.killTimer(timerPanelIdle)
	if p.hwnd == 0 || p.visible() || p.creating {
		return
	}
	p.chromium.Close()
	p.chromium = nil
	destroyWindow(p.hwnd)
	p.hwnd, p.loaded = 0, false
	log.Print("面板空闲,已释放 WebView2")
}

func (p *panel) eval(js string) {
	if p.chromium != nil && p.loaded {
		p.chromium.Eval(js)
	}
}

func (p *panel) wndProc(msg, wp, lp uintptr) (uintptr, bool) {
	switch msg {
	case wmClose: // Alt+F4 只收起面板;退出走菜单或面板按钮
		p.hide()
		return 0, true
	case wmActivate:
		if wp&0xFFFF == waInactive && p.visible() {
			p.hide()
		}
	case wmSize:
		if p.chromium != nil {
			p.chromium.Resize()
		}
	}
	return 0, false
}

// onMessage 在 UI 线程收到面板页的调用;处理函数须快速返回,长动作自行转 goroutine。
func (p *panel) onMessage(raw string) {
	var call struct {
		ID     int               `json:"id"`
		Method string            `json:"method"`
		Params []json.RawMessage `json:"params"`
	}
	if err := json.Unmarshal([]byte(raw), &call); err != nil {
		return
	}
	reply := func(ok bool, value any) {
		data, _ := json.Marshal(value)
		p.eval(fmt.Sprintf("window.__shellReply(%d, %t, %s)", call.ID, ok, data))
	}
	h, found := p.handlers[call.Method]
	if !found {
		reply(false, "未知方法 "+call.Method)
		return
	}
	result, err := h(call.Params)
	if err != nil {
		reply(false, err.Error())
		return
	}
	reply(true, result)
}
