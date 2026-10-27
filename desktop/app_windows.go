package main

import (
	"context"
	"crypto/ed25519"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"runtime/debug"
	"strconv"
	"sync"
	"time"

	"github.com/Nooobhh/ops-agent/desktop/internal/apphost"
	"github.com/Nooobhh/ops-agent/desktop/internal/update"
)

const (
	firstCheckDelay = time.Minute
	checkInterval   = 6 * time.Hour
	healthTimeout   = 180 * time.Second // 版本切换后首启要重建索引,原型实测 16s+10s,留足余量
)

type app struct {
	layout  apphost.Layout
	srv     *apphost.Server
	updater *update.Updater
	keys    []ed25519.PublicKey
	exe     string
	ui      *ui
	panel   *panel
	tray    *tray
	awake   keepAwake
	wake    chan struct{}
	check   chan struct{}

	mu          sync.Mutex
	appVersion  string // 当前应用版本号(与 manifest 比较)
	commit      string
	state       apphost.State
	busy        bool
	openPending bool
	quitting    bool
	overflow    bool

	checking  bool
	lastCheck time.Time
	checkErr  string
	avail     *update.Available
	updating  bool
	progress  string
	percent   int // 下载进度条,-1 = 不显示
	result    string
	resultErr bool
}

type updateView struct {
	Repo        string         `json:"repo"`
	TokenSet    bool           `json:"token_set"`
	Proxy       string         `json:"proxy"`
	Checking    bool           `json:"checking"`
	LastCheck   string         `json:"last_check"`
	Error       string         `json:"error"`
	Available   *availableView `json:"available"`
	Updating    bool           `json:"updating"`
	Progress    string         `json:"progress"`
	Percent     int            `json:"percent"`
	Result      string         `json:"result"`
	ResultError bool           `json:"result_error"`
}

type availableView struct {
	Version string `json:"version"`
	Notes   string `json:"notes"`
}

type panelState struct {
	State     apphost.State `json:"state"`
	Label     string        `json:"label"`
	Error     string        `json:"error"`
	Version   string        `json:"version"`
	Shell     string        `json:"shell"`
	Busy      bool          `json:"busy"`
	Quitting  bool          `json:"quitting"`
	Autostart bool          `json:"autostart"`
	Overflow  bool          `json:"overflow"`
	Update    updateView    `json:"update"`
}

func (a *app) configPath() string { return filepath.Join(a.layout.ConfigDir(), update.ConfigName) }

// recallLastUpdate 外壳重启(更新交接、回退收尾)后,面板接着显示最近一次更新的结果,一小时后不再提。
func (a *app) recallLastUpdate() {
	rec, err := a.updater.LoadRecord()
	if err != nil || rec.From == "" {
		return
	}
	at, err := time.Parse(time.RFC3339, rec.UpdatedAt)
	if err != nil || time.Since(at) > time.Hour {
		return
	}
	switch rec.Phase {
	case update.PhaseDone:
		a.result = "已更新到 " + rec.To
	case update.PhaseFailed:
		a.result, a.resultErr = "上次更新失败:"+rec.Error, true
	}
}

func (a *app) run(start, openBrowser bool) error {
	u, err := newUI()
	if err != nil {
		return fmt.Errorf("创建消息窗口失败: %w", err)
	}
	a.ui = u
	a.tray = newTray(u.hwnd)
	a.panel, err = newPanel(u, filepath.Join(a.layout.ShellDir(), "webview2"), a.handlers())
	if err != nil {
		return err
	}
	u.onTray = a.onTray
	u.onShowRequest = a.onShowRequest
	u.onQuitRequest = a.quit
	u.onTaskbarCreated = func() { _ = a.tray.add(a.currentState(), a.hasUpdate(), a.tooltip()) }
	u.onTimer = func(id uintptr) {
		if id == timerPanelIdle {
			a.panel.destroyIfIdle()
		}
	}
	// 拿不到托盘(explorer 未就绪、无交互桌面)不退出:中台照常托管,explorer 起来后广播 TaskbarCreated 再补
	a.recallLastUpdate()
	if err := a.tray.add(apphost.StateStopped, false, a.tooltip()); err != nil {
		log.Print(err)
	}
	safeGo(a.monitor)
	safeGo(a.checkLoop)
	if start {
		a.mu.Lock()
		a.openPending = openBrowser
		a.mu.Unlock()
		a.act(func() { _ = a.srv.Start() })
	}
	log.Printf("外壳 %s 已启动,应用 %s,安装目录 %s", shellBuild, a.versionLabel(), a.layout.Root)
	u.run()
	a.tray.remove()
	a.awake.set(false)
	log.Print("外壳退出")
	return nil
}

func (a *app) handlers() map[string]handler {
	noArgs := func(f func()) handler {
		return func([]json.RawMessage) (any, error) { f(); return nil, nil }
	}
	return map[string]handler{
		"state":       func([]json.RawMessage) (any, error) { return a.snapshot(), nil },
		"start":       noArgs(func() { a.act(func() { _ = a.srv.Start() }) }),
		"stop":        noArgs(func() { a.act(a.srv.Stop) }),
		"restart":     noArgs(func() { a.act(func() { a.srv.Stop(); _ = a.srv.Start() }) }),
		"openConsole": noArgs(func() { shellOpen(a.srv.ConsoleURL()) }),
		"openLog": noArgs(func() {
			if _, err := os.Stat(a.srv.LogPath()); err == nil {
				shellOpen(a.srv.LogPath())
			} else {
				shellOpen(a.srv.StateDir)
			}
		}),
		"hide":        noArgs(func() { a.panel.hide() }),
		"quit":        noArgs(a.quit),
		"checkUpdate": noArgs(a.requestCheck),
		"applyUpdate": func(p []json.RawMessage) (any, error) {
			var confirmed bool
			if len(p) > 0 {
				_ = json.Unmarshal(p[0], &confirmed)
			}
			return a.applyUpdate(confirmed)
		},
		"saveUpdateConfig": func(p []json.RawMessage) (any, error) {
			var in struct {
				Repo       string `json:"repo"`
				Token      string `json:"token"`
				Proxy      string `json:"proxy"`
				ClearToken bool   `json:"clear_token"`
			}
			if len(p) == 0 || json.Unmarshal(p[0], &in) != nil {
				return nil, errors.New("参数不对")
			}
			return nil, a.saveUpdateConfig(in.Repo, in.Token, in.Proxy, in.ClearToken)
		},
		"setAutostart": func(p []json.RawMessage) (any, error) {
			var on bool
			if len(p) > 0 {
				_ = json.Unmarshal(p[0], &on)
			}
			return nil, setAutostart(a.exe, on)
		},
	}
}

// ---- 状态 ----

// monitor 后台轮询中台状态;托盘与电源请求都可跨线程调用。
func (a *app) monitor() {
	for {
		state := a.srv.Refresh()
		a.mu.Lock()
		changed := state != a.state
		a.state = state
		openNow := changed && state == apphost.StateRunning && a.openPending
		if openNow {
			a.openPending = false
		}
		a.mu.Unlock()
		a.awake.set(state == apphost.StateRunning || state == apphost.StateStarting || state == apphost.StateUnresponsive)
		if changed {
			log.Printf("中台状态 -> %s %s", state, a.srv.LastError())
			a.refreshTray()
		}
		if openNow {
			a.ui.dispatch(func() { shellOpen(a.srv.ConsoleURL()) })
		}
		select {
		case <-a.wake:
		case <-time.After(2 * time.Second):
		}
	}
}

func (a *app) refreshTray() { a.tray.update(a.currentState(), a.hasUpdate(), a.tooltip()) }

// act 串行执行起停/更新类动作,不阻塞 UI 线程;已有动作在跑或正在退出时忽略。
func (a *app) act(f func()) bool {
	a.mu.Lock()
	if a.busy || a.quitting {
		a.mu.Unlock()
		return false
	}
	a.busy = true
	a.mu.Unlock()
	safeGo(func() {
		defer func() {
			a.mu.Lock()
			a.busy = false
			a.mu.Unlock()
			a.poke()
		}()
		f()
	})
	a.poke()
	return true
}

func (a *app) poke() {
	select {
	case a.wake <- struct{}{}:
	default:
	}
}

func (a *app) currentState() apphost.State {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.state
}

func (a *app) hasUpdate() bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.avail != nil
}

func (a *app) versionLabel() string {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.commit != "" {
		return a.appVersion + " (" + a.commit + ")"
	}
	return a.appVersion
}

func (a *app) tooltip() string {
	tip := windowTitle + ":" + apphost.StateLabels[a.currentState()]
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.avail != nil {
		tip += ",有新版本 " + a.avail.Manifest.Version
	}
	return tip
}

func (a *app) snapshot() panelState {
	cfg, _ := update.LoadConfig(a.configPath())
	autostart := autostartEnabled(a.exe)
	version := a.versionLabel()
	a.mu.Lock()
	defer a.mu.Unlock()
	view := updateView{
		Repo: cfg.Repo, TokenSet: cfg.Token != "", Proxy: cfg.Proxy, Checking: a.checking, Error: a.checkErr,
		Updating: a.updating, Progress: a.progress, Percent: a.percent, Result: a.result, ResultError: a.resultErr,
	}
	if !a.lastCheck.IsZero() {
		view.LastCheck = a.lastCheck.Format("01-02 15:04")
	}
	if a.avail != nil {
		view.Available = &availableView{Version: a.avail.Manifest.Version, Notes: a.avail.Manifest.Notes}
	}
	return panelState{
		State: a.state, Label: apphost.StateLabels[a.state], Error: a.srv.LastError(),
		Version: version, Shell: shellBuild, Busy: a.busy, Quitting: a.quitting,
		Autostart: autostart, Overflow: a.overflow, Update: view,
	}
}

// ---- 托盘与面板 ----

func (a *app) onTray(event uintptr, x, y int32) {
	switch event {
	case ninSelect, ninKeySelect:
		_, _, overflow := a.tray.anchor()
		a.setOverflow(overflow)
		a.panel.toggle(x, y)
	case wmContextMenu:
		a.contextMenu(x, y)
	}
}

func (a *app) setOverflow(v bool) {
	a.mu.Lock()
	a.overflow = v
	a.mu.Unlock()
}

func (a *app) showPanel() {
	x, y, overflow := a.tray.anchor()
	a.setOverflow(overflow)
	a.panel.showAt(x, y)
}

func (a *app) contextMenu(x, y int32) {
	state := a.currentState()
	live := state == apphost.StateRunning || state == apphost.StateUnresponsive
	items := []menuItem{{1, "打开面板", true}, {2, "打开控制台", live}, {3, "检查更新", true}, {}, {9, "退出(停止中台)", true}}
	switch a.tray.popup(x, y, items) {
	case 1:
		a.showPanel()
	case 2:
		shellOpen(a.srv.ConsoleURL())
	case 3:
		a.requestCheck()
		a.showPanel()
	case 9:
		a.quit()
	}
}

// onShowRequest 处理再次双击 exe:弹出面板,中台停着则拉起。
func (a *app) onShowRequest() {
	a.showPanel()
	if state := a.currentState(); state == apphost.StateStopped || state == apphost.StateFailed {
		a.act(func() { _ = a.srv.Start() })
	}
}

func (a *app) quit() { a.exit(true) }

// exit stopServer=false 用于外壳交接:中台留给新外壳接管。
func (a *app) exit(stopServer bool) {
	a.mu.Lock()
	if a.quitting {
		a.mu.Unlock()
		return
	}
	a.quitting = true
	a.mu.Unlock()
	log.Printf("退出(停止中台=%v)", stopServer)
	safeGo(func() {
		// 等在途动作(如启动)结束再停,否则停止可能跑在拉起之前;交接时自身就在动作里,不等
		for stopServer {
			a.mu.Lock()
			busy := a.busy
			a.mu.Unlock()
			if !busy {
				break
			}
			time.Sleep(200 * time.Millisecond)
		}
		if stopServer {
			a.srv.Stop()
		}
		a.ui.dispatch(func() {
			a.panel.hide()
			a.tray.remove()
			quitLoop()
		})
	})
}

// ---- 更新 ----

func (a *app) requestCheck() {
	select {
	case a.check <- struct{}{}:
	default:
	}
}

func (a *app) checkLoop() {
	timer := time.NewTimer(firstCheckDelay)
	for {
		select {
		case <-timer.C:
		case <-a.check:
		}
		a.checkUpdate()
		timer.Reset(checkInterval)
	}
}

func (a *app) checkUpdate() {
	a.mu.Lock()
	if a.updating || a.checking {
		a.mu.Unlock()
		return
	}
	a.checking = true
	current := a.appVersion
	a.mu.Unlock()

	var avail *update.Available
	cfg, err := update.LoadConfig(a.configPath())
	var client *update.Client
	if err == nil {
		client, err = update.NewClient(cfg)
	}
	if err == nil {
		avail, err = update.Check(context.Background(), client, a.keys, current)
	}
	a.mu.Lock()
	a.checking, a.lastCheck = false, time.Now()
	// 以最近一次检查为准:最新 release 有问题(如验签失败)时不再挂着之前发现的版本
	a.checkErr, a.avail = "", avail
	if err != nil {
		a.checkErr = err.Error()
		log.Printf("检查更新失败: %v", err)
	} else if avail != nil {
		log.Printf("发现新版本 %s", avail.Manifest.Version)
	}
	a.mu.Unlock()
	a.refreshTray()
}

func (a *app) saveUpdateConfig(repo, token, proxy string, clearToken bool) error {
	old, _ := update.LoadConfig(a.configPath())
	cfg := update.Config{Repo: repo, Token: token, Proxy: proxy}
	if token == "" && !clearToken {
		cfg.Token = old.Token // 只写不回显:留空沿用原值
	}
	if err := update.SaveConfig(a.configPath(), cfg); err != nil {
		return err
	}
	a.requestCheck()
	return nil
}

func (a *app) applyUpdate(confirmed bool) (any, error) {
	a.mu.Lock()
	avail, blocked := a.avail, a.busy || a.updating || a.quitting
	a.mu.Unlock()
	if avail == nil {
		return nil, errors.New("没有可用的新版本")
	}
	if blocked {
		return nil, errors.New("正在执行其他操作,稍后再试")
	}
	if !confirmed {
		activity, err := a.srv.Activity()
		if err != nil {
			log.Printf("读取中台活动失败(不拦更新): %v", err)
		}
		if activity.Busy() {
			return map[string]any{"confirm": activity}, nil
		}
	}
	if !a.act(func() { a.runUpdate(avail) }) {
		return nil, errors.New("正在执行其他操作,稍后再试")
	}
	return map[string]any{"started": true}, nil
}

func (a *app) setProgress(text string) {
	a.mu.Lock()
	a.progress = text
	a.mu.Unlock()
}

func (a *app) setPercent(pct int) {
	a.mu.Lock()
	a.percent = pct
	a.mu.Unlock()
}

func (a *app) runUpdate(avail *update.Available) {
	a.mu.Lock()
	a.updating, a.result, a.resultErr, a.progress, a.percent = true, "", false, "准备下载", -1
	a.mu.Unlock()
	finish := func(result string, isErr bool) {
		a.mu.Lock()
		a.updating, a.progress, a.percent, a.result, a.resultErr = false, "", -1, result, isErr
		a.mu.Unlock()
		a.refreshTray()
	}
	cfg, err := update.LoadConfig(a.configPath())
	var client *update.Client
	if err == nil {
		client, err = update.NewClient(cfg)
	}
	if err != nil {
		finish(err.Error(), true)
		return
	}
	a.updater.Source = client
	a.updater.Progress = a.setProgress
	a.updater.Percent = a.setPercent
	to := avail.Manifest.Version
	if err := a.updater.Apply(context.Background(), avail); err != nil {
		log.Printf("更新失败: %v", err)
		finish("更新失败:"+err.Error(), true)
		return
	}
	info, _ := apphost.ReadPackageInfo(a.layout.AppDir(to))
	a.mu.Lock()
	a.appVersion, a.commit, a.avail = to, info.Commit, nil
	a.mu.Unlock()
	log.Printf("已更新到 %s", to)
	if !update.NeedsShell(avail.Manifest, a.exe) {
		finish("已更新到 "+to, false)
		return
	}
	a.setProgress("下载新外壳")
	newExe, err := a.updater.FetchShell(context.Background(), avail)
	if err == nil {
		err = a.handover(newExe)
	}
	if err != nil {
		log.Printf("外壳替换失败: %v", err)
		finish("已更新到 "+to+";外壳替换失败("+err.Error()+"),下次更新再试", true)
		return
	}
	finish("已更新到 "+to+",外壳重启中", false)
}

// handover 换外壳:运行中的 exe 改名 .old → 放新 exe → 拉起新外壳 → 等它报到 → 自己退出但不停中台。
func (a *app) handover(newExe string) error {
	ev, err := createHandoverEvent()
	if err != nil {
		return err
	}
	defer closeHandle(ev)
	old := a.exe + ".old"
	_ = os.Remove(old)
	if err := os.Rename(a.exe, old); err != nil {
		return err
	}
	restore := func() {
		_ = os.Remove(a.exe)
		_ = os.Rename(old, a.exe)
	}
	if err := copyFile(newExe, a.exe); err != nil {
		restore()
		return err
	}
	cmd := exec.Command(a.exe, "--handover", "--no-browser", "--root", a.layout.Root, "--port", strconv.Itoa(a.srv.Port))
	if err := cmd.Start(); err != nil {
		restore()
		return err
	}
	if !waitEvent(ev, 20000) {
		_ = cmd.Process.Kill()
		restore()
		return errors.New("新外壳 20 秒内没有报到")
	}
	_ = cmd.Process.Release()
	log.Print("新外壳已报到,本外壳退出,中台交给新外壳")
	a.exit(false)
	return nil
}

func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(dst, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o755)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}

// ---- 杂项 ----

func logf(format string, args ...any) { log.Printf(format, args...) }

// safeGo goroutine panic 记堆栈后退出外壳;中台不受影响,外壳重启后按 holder 接管。
func safeGo(f func()) {
	go func() {
		defer func() {
			if r := recover(); r != nil {
				log.Printf("外壳 panic: %v\n%s", r, debug.Stack())
				os.Exit(2)
			}
		}()
		f()
	}()
}
