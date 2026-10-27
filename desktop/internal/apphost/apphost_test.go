package apphost

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// 测试二进制兼当假中台:Server 拉起的「python」就是它自己(APPHOST_FAKE=1)。
func TestMain(m *testing.M) {
	if os.Getenv("APPHOST_FAKE") == "1" {
		os.Exit(fakePython(os.Args[1:]))
	}
	os.Exit(m.Run())
}

func fakePython(args []string) int {
	if len(args) >= 2 && strings.HasSuffix(args[0], "opsagent.py") && args[1] == "prepare" {
		if os.Getenv("APPHOST_FAKE_PREPARE_FAIL") == "1" {
			fmt.Println(`{"ok": false, "error": "prepare_failed", "message": "索引坏了"}`)
			return 1
		}
		fmt.Println(`{"ok": true}`)
		return 0
	}
	var port, stateDir string
	for i := 1; i+1 < len(args); i++ {
		switch args[i] {
		case "--port":
			port = args[i+1]
		case "--state-dir":
			stateDir = args[i+1]
		}
	}
	if os.Getenv("APPHOST_FAKE_CRASH") == "1" {
		fmt.Println(`{"ok": false, "error": "port_in_use", "message": "端口被占用"}`)
		return 1
	}
	_ = os.WriteFile(filepath.Join(stateDir, "env-data-root"), []byte(os.Getenv("OPS_AGENT_DATA_ROOT")), 0o644)
	holder, _ := json.Marshal(map[string]any{"pid": os.Getpid(), "port": port})
	_ = os.WriteFile(filepath.Join(stateDir, holderFile), holder, 0o644)
	mux := http.NewServeMux()
	mux.HandleFunc("/api/health", func(w http.ResponseWriter, _ *http.Request) { fmt.Fprint(w, `{"status":"ok"}`) })
	mux.HandleFunc("/api/triage-health", func(w http.ResponseWriter, _ *http.Request) {
		fmt.Fprint(w, `{"queue":{"queued":1,"running":1,"waiting_input":4,"waiting_operation":1}}`)
	})
	mux.HandleFunc("/api/weekly-report-jobs", func(w http.ResponseWriter, _ *http.Request) {
		fmt.Fprint(w, `{"items":[{"status":"running"},{"status":"completed"}]}`)
	})
	ln, err := net.Listen("tcp", "127.0.0.1:"+port)
	if err != nil {
		return 2
	}
	go http.Serve(ln, mux)
	for {
		if _, err := os.Stat(filepath.Join(stateDir, stopRequest)); err == nil && os.Getenv("APPHOST_FAKE_IGNORE_STOP") != "1" {
			return 0
		}
		time.Sleep(50 * time.Millisecond)
	}
}

func freePort(t *testing.T) int {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	return ln.Addr().(*net.TCPAddr).Port
}

func newFakeServer(t *testing.T, env ...string) (*Server, string) {
	t.Setenv("APPHOST_FAKE", "1")
	for i := 0; i+1 < len(env); i += 2 {
		t.Setenv(env[i], env[i+1])
	}
	root := t.TempDir()
	layout := Layout{Root: root}
	appDir := layout.AppDir("1.0.0")
	if err := os.MkdirAll(appDir, 0o755); err != nil {
		t.Fatal(err)
	}
	exe, _ := os.Executable()
	s := NewServer(appDir, layout.DataDir(), freePort(t))
	s.PythonFor = func(string) string { return exe }
	s.StopTimeout = 3 * time.Second
	t.Cleanup(func() {
		if pid, owned := s.pid(); owned {
			killTree(pid)
		}
	})
	return s, root
}

func TestLayoutCurrentAndVersions(t *testing.T) {
	l := Layout{Root: t.TempDir()}
	if _, err := l.Current(); err == nil {
		t.Fatal("缺 current 应报错")
	}
	for _, v := range []string{"1.0.0", "1.1.0", "1.2.0.tmp"} {
		_ = os.MkdirAll(filepath.Dir(PythonPath(l.AppDir(v))), 0o755)
		_ = os.WriteFile(PythonPath(l.AppDir(v)), nil, 0o755)
		_ = os.WriteFile(filepath.Join(l.AppDir(v), PackageInfoName), []byte(`{"version":"`+v+`"}`), 0o644)
	}
	if err := l.SetCurrent("1.1.0"); err != nil {
		t.Fatal(err)
	}
	// 兼容记事本保存的 BOM 与换行
	_ = os.WriteFile(filepath.Join(l.AppRoot(), "current"), []byte("\xef\xbb\xbf1.1.0\r\n"), 0o644)
	if v, err := l.Current(); err != nil || v != "1.1.0" {
		t.Fatalf("Current = %q, %v", v, err)
	}
	if got, _ := l.Versions(); strings.Join(got, ",") != "1.0.0,1.1.0" {
		t.Fatalf("Versions = %v", got)
	}
	for _, bad := range []string{"", "..", "a/b", `a\b`, "x.tmp", ".hidden", "c:x"} {
		if l.SetCurrent(bad) == nil {
			t.Fatalf("SetCurrent(%q) 应拒绝", bad)
		}
	}
	info, err := ReadPackageInfo(l.AppDir("1.0.0"))
	if err != nil || info.Version != "1.0.0" {
		t.Fatalf("ReadPackageInfo = %+v, %v", info, err)
	}
}

func TestEnsureDirsLeavesKnowledgeForSeed(t *testing.T) {
	l := Layout{Root: t.TempDir()}
	if err := l.EnsureDirs(); err != nil {
		t.Fatal(err)
	}
	for _, dir := range []string{l.ShellDir(), l.ConfigDir(), l.StateDir()} {
		if _, err := os.Stat(dir); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := os.Stat(filepath.Join(l.DataDir(), "knowledge")); !os.IsNotExist(err) {
		t.Fatal("knowledge 不应预建:知识库由使用者放进数据目录")
	}
}

func TestStartStopInjectsDataRoot(t *testing.T) {
	s, root := newFakeServer(t)
	if err := s.Start(); err != nil {
		t.Fatal(err)
	}
	if !s.WaitHealthy(context.Background(), 10*time.Second) {
		t.Fatalf("未就绪: %s", s.LastError())
	}
	got, _ := os.ReadFile(filepath.Join(s.StateDir, "env-data-root"))
	if string(got) != filepath.Join(root, "data") {
		t.Fatalf("OPS_AGENT_DATA_ROOT = %q", got)
	}
	if err := s.Start(); err != nil { // 已在跑:不重复拉起
		t.Fatal(err)
	}
	activity, err := s.Activity()
	if err != nil || activity != (Activity{Tasks: 3, WeeklyJobs: 1}) || !activity.Busy() {
		t.Fatalf("Activity = %+v, %v", activity, err)
	}
	started := time.Now()
	s.Stop()
	if time.Since(started) > 2*time.Second {
		t.Fatal("停止请求文件应让中台正常退出,不该等到强杀")
	}
	if state := s.Refresh(); state != StateStopped {
		t.Fatalf("停止后状态 %s", state)
	}
	if _, err := os.Stat(filepath.Join(s.StateDir, stopRequest)); !os.IsNotExist(err) {
		t.Fatal("停止请求文件应清掉")
	}
}

func TestStopKillsTreeAfterTimeout(t *testing.T) {
	s, _ := newFakeServer(t, "APPHOST_FAKE_IGNORE_STOP", "1")
	s.StopTimeout = 500 * time.Millisecond
	_ = s.Start()
	if !s.WaitHealthy(context.Background(), 10*time.Second) {
		t.Fatal("未就绪")
	}
	s.Stop()
	if s.Healthy() {
		t.Fatal("超时后应强杀")
	}
}

func TestStartFailureSurfacesMessage(t *testing.T) {
	s, _ := newFakeServer(t, "APPHOST_FAKE_CRASH", "1")
	_ = s.Start()
	if s.WaitHealthy(context.Background(), 10*time.Second) {
		t.Fatal("假中台应启动失败")
	}
	if s.Refresh() != StateFailed || s.LastError() != "端口被占用" {
		t.Fatalf("状态 %s 错误 %q", s.Refresh(), s.LastError())
	}
}

func TestPrepareFailureDoesNotBlockStart(t *testing.T) {
	s, _ := newFakeServer(t, "APPHOST_FAKE_PREPARE_FAIL", "1")
	_ = os.WriteFile(filepath.Join(s.AppDir(), PackageInfoName), []byte(`{"version":"1.0.0"}`), 0o644)
	if msg := s.prepare(s.AppDir()); msg != "知识库准备失败: 索引坏了" {
		t.Fatalf("prepare = %q", msg)
	}
	_ = s.Start()
	if !s.WaitHealthy(context.Background(), 10*time.Second) {
		t.Fatal("准备失败不应拦启动")
	}
	s.Stop()
}

func TestAdoptsExternalInstanceViaHolder(t *testing.T) {
	a, _ := newFakeServer(t)
	_ = a.Start()
	if !a.WaitHealthy(context.Background(), 10*time.Second) {
		t.Fatal("未就绪")
	}
	b := NewServer(a.AppDir(), a.DataDir, a.Port) // 另一个外壳实例(交接后的新外壳)
	if pid, owned := b.pid(); owned || pid == 0 {
		t.Fatalf("应按 holder 接管 pid=%d owned=%v", pid, owned)
	}
	b.Stop()
	if a.Healthy() {
		t.Fatal("接管方也能正常停掉外部实例")
	}
}
