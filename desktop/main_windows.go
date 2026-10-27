// OpsAgent.exe:Windows 桌面外壳。托盘图标 + 面板,托管 app\<版本>\ 下的 Python 中台,签名更新。
// 方案见 docs/specs/2026-10-21-windows-desktop-shell.md 与 2026-10-25-m9-desktop-cli.md。
package main

import (
	"context"
	_ "embed"
	"flag"
	"log"
	"os"
	"path/filepath"
	"runtime"
	"runtime/debug"
	"time"

	"golang.org/x/sys/windows"

	"github.com/Nooobhh/ops-agent/desktop/internal/apphost"
	"github.com/Nooobhh/ops-agent/desktop/internal/release"
	"github.com/Nooobhh/ops-agent/desktop/internal/update"
)

const windowTitle = "ops-agent 中台"

// shellBuild 由构建脚本经 -ldflags -X 注入 desktop/ 目录的 git tree 短哈希;
// 不注入版本号,保证 desktop/ 不变时 exe 字节不变、更新不换外壳。
var shellBuild = "dev"

//go:embed release.pub
var releasePub string

// 消息循环与 WebView2 都要求固定在创建它们的线程上
func init() { runtime.LockOSThread() }

func main() {
	exe, _ := os.Executable()
	root := flag.String("root", filepath.Dir(exe), "安装根目录(含 app\\ data\\ shell\\)")
	port := flag.Int("port", 8420, "中台端口")
	noStart := flag.Bool("no-start", false, "启动外壳时不自动拉起中台")
	noBrowser := flag.Bool("no-browser", false, "中台就绪后不自动打开控制台")
	handover := flag.Bool("handover", false, "由旧外壳拉起接管(内部用)")
	quit := flag.Bool("quit", false, "让已运行的外壳停中台并退出(脚本用),最多等 90 秒")
	flag.DurationVar(&panelIdle, "panel-idle", panelIdle, "面板隐藏多久后释放 WebView2(验收用)")
	flag.Parse()
	if *quit {
		if !requestQuit(90000) {
			os.Exit(1)
		}
		return
	}

	layout := apphost.Layout{Root: *root}
	if err := layout.EnsureDirs(); err != nil {
		messageBox("无法创建数据目录: " + err.Error())
		return
	}
	logFile := openShellLog(layout)
	defer logFile.Close()
	defer func() {
		if r := recover(); r != nil {
			log.Printf("外壳 panic: %v\n%s", r, debug.Stack())
			os.Exit(2)
		}
	}()

	if *handover {
		signalHandover()
	}
	wait := 0
	if *handover {
		wait = 30000
	}
	mutex, ok := acquireMutex(wait)
	if !ok {
		if *handover {
			log.Print("交接:旧外壳 30 秒内未退出,放弃")
			return
		}
		log.Printf("外壳已在运行,已通知其弹出面板 hwnd=%#x", notifyRunning())
		return
	}
	defer windows.CloseHandle(mutex)
	_ = windows.CoInitializeEx(0, windows.COINIT_APARTMENTTHREADED)
	procSetProcessDpiAwarenessContext.Call(dpiAwarenessPerMonitorV2)
	safeGo(func() { removeOldExe(exe) })

	keys, err := release.ParsePublicKeys(releasePub)
	if err != nil {
		log.Printf("内置公钥无效,更新不可用: %v", err)
	}
	version, err := layout.Current()
	if err != nil {
		log.Print(err)
		messageBox(err.Error())
		return
	}
	srv := apphost.NewServer(layout.AppDir(version), layout.DataDir(), *port)
	updater := &update.Updater{Layout: layout, Host: srv, HealthTimeout: healthTimeout, Logf: log.Printf}
	// 上次更新停在半路(外壳崩溃/断电):先收尾再拉起中台
	if err := updater.Recover(context.Background()); err != nil {
		log.Printf("更新收尾失败: %v", err)
	}
	if version, err = layout.Current(); err != nil {
		messageBox(err.Error())
		return
	}
	srv.SetAppDir(layout.AppDir(version))
	info, _ := apphost.ReadPackageInfo(layout.AppDir(version))
	if info.Version == "" {
		info.Version = version
	}

	a := &app{
		layout: layout, srv: srv, updater: updater, keys: keys, exe: exe,
		appVersion: info.Version, commit: info.Commit, state: apphost.StateStopped, percent: -1,
		wake: make(chan struct{}, 1), check: make(chan struct{}, 1),
	}
	if err := a.run(!*noStart, !*noBrowser && !*handover); err != nil {
		log.Print(err)
		messageBox(err.Error())
	}
}

func openShellLog(layout apphost.Layout) *os.File {
	path := filepath.Join(layout.ShellDir(), "shell.log")
	if info, err := os.Stat(path); err == nil && info.Size() > 5<<20 {
		_ = os.Rename(path, path+".1")
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return nil
	}
	log.SetOutput(f)
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
	return f
}

// removeOldExe 删上次交接留下的 OpsAgent.exe.old;旧外壳可能还在退出,重试一会儿。
func removeOldExe(exe string) {
	old := exe + ".old"
	for i := 0; i < 30; i++ {
		if err := os.Remove(old); err == nil || os.IsNotExist(err) {
			return
		}
		time.Sleep(2 * time.Second)
	}
}
