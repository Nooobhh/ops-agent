package update

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/Nooobhh/ops-agent/desktop/internal/apphost"
	"github.com/Nooobhh/ops-agent/desktop/internal/release"
)

// ---- 测试素材 ----

type fixture struct {
	keys  []ed25519.PublicKey
	priv  ed25519.PrivateKey
	files map[string][]byte // release 资产名 → 内容
	rel   *Release
}

func appZip(t *testing.T, version string, extra map[string]string) []byte {
	var buf bytes.Buffer
	zw := zip.NewWriter(&buf)
	files := map[string]string{
		"package-info.json":   fmt.Sprintf(`{"version":%q}`, version),
		"runtime/bin/python3": "", "runtime/python.exe": "",
		"scripts/web_server.py": "",
	}
	for k, v := range extra {
		files[k] = v
	}
	for name, body := range files {
		w, err := zw.Create(name)
		if err != nil {
			t.Fatal(err)
		}
		io.WriteString(w, body)
	}
	zw.Close()
	return buf.Bytes()
}

func newFixture(t *testing.T, version string, mutate func(m *release.Manifest)) *fixture {
	pubLine, privText, _ := release.GenerateKey()
	priv, _ := release.ParsePrivateKey(privText)
	keys, _ := release.ParsePublicKeys(pubLine)
	f := &fixture{keys: keys, priv: priv, files: map[string][]byte{}}
	app := appZip(t, version, nil)
	shell := []byte("new shell exe " + version)
	f.files["OpsAgent-app.zip"] = app
	f.files["OpsAgent-shell.exe"] = shell
	m := release.Manifest{Version: version, ShellSHA256: sum(shell), Assets: []release.Asset{
		{Name: "OpsAgent-app.zip", Kind: release.KindApp, Size: int64(len(app)), SHA256: sum(app)},
		{Name: "OpsAgent-shell.exe", Kind: release.KindShell, Size: int64(len(shell)), SHA256: sum(shell)},
	}}
	if mutate != nil {
		mutate(&m)
	}
	data, _ := json.Marshal(m)
	f.files[release.ManifestName] = data
	f.files[release.SignatureName] = []byte(release.Sign(priv, data))
	f.rel = &Release{TagName: "v" + version}
	id := int64(1)
	for name, body := range f.files {
		f.rel.Assets = append(f.rel.Assets, ReleaseAsset{ID: id, Name: name, Size: int64(len(body))})
		id++
	}
	return f
}

func sum(b []byte) string {
	h := sha256.Sum256(b)
	return hex.EncodeToString(h[:])
}

func (f *fixture) Latest(context.Context) (*Release, error) { return f.rel, nil }

func (f *fixture) Download(_ context.Context, a ReleaseAsset, w io.Writer) error {
	_, err := w.Write(f.files[a.Name])
	return err
}

type fakeHost struct {
	log       []string
	appDir    string
	healthyAt func(appDir string) bool
}

func (h *fakeHost) Stop()                { h.log = append(h.log, "stop") }
func (h *fakeHost) SetAppDir(dir string) { h.appDir = dir }
func (h *fakeHost) Start() error {
	h.log = append(h.log, "start "+filepath.Base(h.appDir))
	return nil
}
func (h *fakeHost) WaitHealthy(context.Context, time.Duration) bool { return h.healthyAt(h.appDir) }

func installed(t *testing.T, version string) apphost.Layout {
	l := apphost.Layout{Root: t.TempDir()}
	for _, p := range []string{apphost.PythonPath(l.AppDir(version)), filepath.Join(l.AppDir(version), apphost.PackageInfoName)} {
		os.MkdirAll(filepath.Dir(p), 0o755)
		os.WriteFile(p, []byte(fmt.Sprintf(`{"version":%q}`, version)), 0o644)
	}
	if err := l.EnsureDirs(); err != nil {
		t.Fatal(err)
	}
	if err := l.SetCurrent(version); err != nil {
		t.Fatal(err)
	}
	os.WriteFile(filepath.Join(l.StateDir(), "triage-settings.json"), []byte("old"), 0o644)
	os.WriteFile(filepath.Join(l.StateDir(), "web_server.log"), []byte("log"), 0o644)
	os.MkdirAll(filepath.Join(l.StateDir(), "graphs"), 0o755)
	os.WriteFile(filepath.Join(l.StateDir(), "graphs", "big.json"), []byte("big"), 0o644)
	return l
}

func newUpdater(l apphost.Layout, f *fixture, healthy func(string) bool) (*Updater, *fakeHost) {
	host := &fakeHost{appDir: l.AppDir("1.0.0"), healthyAt: healthy}
	return &Updater{Layout: l, Host: host, Source: f, HealthTimeout: time.Second}, host
}

// ---- Check ----

func TestCheckVerifiesAndComparesVersions(t *testing.T) {
	f := newFixture(t, "1.0.0-m9.2", nil)
	avail, err := Check(context.Background(), f, f.keys, "1.0.0-m9.1")
	if err != nil || avail == nil || avail.Manifest.Version != "1.0.0-m9.2" {
		t.Fatalf("Check = %+v, %v", avail, err)
	}
	if avail, err := Check(context.Background(), f, f.keys, "1.0.0-m9.2"); avail != nil || err != nil {
		t.Fatalf("同版本不应提示更新: %+v %v", avail, err)
	}
	other, _, _ := release.GenerateKey()
	otherKeys, _ := release.ParsePublicKeys(other)
	if _, err := Check(context.Background(), f, otherKeys, "1.0.0-m9.1"); ErrorKind(err) != "bad_release" {
		t.Fatalf("错公钥应拒绝: %v", err)
	}
	f.files[release.ManifestName] = bytes.Replace(f.files[release.ManifestName], []byte("m9.2"), []byte("m9.3"), 1)
	if _, err := Check(context.Background(), f, f.keys, "1.0.0-m9.1"); err == nil || !strings.Contains(err.Error(), "签名") {
		t.Fatalf("篡改 manifest 应验签失败: %v", err)
	}
	f = newFixture(t, "1.0.0-m9.2", nil)
	f.rel.TagName = "v9.9.9"
	if _, err := Check(context.Background(), f, f.keys, "1.0.0-m9.1"); ErrorKind(err) != "bad_release" {
		t.Fatalf("标签不一致应拒绝: %v", err)
	}
}

// ---- Apply ----

func TestApplySwitchesAndCleansUp(t *testing.T) {
	l := installed(t, "1.0.0")
	for _, old := range []string{"0.9.0", "0.8.0"} {
		os.MkdirAll(l.AppDir(old), 0o755)
		os.WriteFile(filepath.Join(l.AppDir(old), apphost.PackageInfoName), []byte(`{"version":"x"}`), 0o644)
	}
	f := newFixture(t, "1.1.0", nil)
	u, host := newUpdater(l, f, func(dir string) bool { return filepath.Base(dir) == "1.1.0" })
	avail, _ := Check(context.Background(), f, f.keys, "1.0.0")
	if err := u.Apply(context.Background(), avail); err != nil {
		t.Fatal(err)
	}
	if cur, _ := l.Current(); cur != "1.1.0" {
		t.Fatalf("current = %s", cur)
	}
	if strings.Join(host.log, ",") != "stop,start 1.1.0" {
		t.Fatalf("host 调用顺序 %v", host.log)
	}
	if v, _ := l.Versions(); strings.Join(v, ",") != "1.0.0,1.1.0" {
		t.Fatalf("应只留当前与上一版: %v", v)
	}
	rec, _ := u.LoadRecord()
	if rec.Phase != PhaseDone || rec.From != "1.0.0" || rec.To != "1.1.0" {
		t.Fatalf("record %+v", rec)
	}
	if _, err := os.Stat(filepath.Join(l.UpdatesDir(), "1.1.0")); !os.IsNotExist(err) {
		t.Fatal("成功后下载包应清理")
	}
	if _, err := os.Stat(rec.Snapshot); err != nil {
		t.Fatal("上一版的 state 快照应保留")
	}
}

func TestDownloadReportsPercentForProgressBar(t *testing.T) {
	l := installed(t, "1.0.0")
	f := newFixture(t, "1.1.0", nil)
	u, _ := newUpdater(l, f, func(dir string) bool { return filepath.Base(dir) == "1.1.0" })
	var pcts []int
	var texts []string
	u.Percent = func(p int) { pcts = append(pcts, p) }
	u.Progress = func(s string) { texts = append(texts, s) }
	avail, _ := Check(context.Background(), f, f.keys, "1.0.0")
	if err := u.Apply(context.Background(), avail); err != nil {
		t.Fatal(err)
	}
	// 进度条:开始 0、下载到 100、离开下载阶段 -1(面板据此收起进度条)
	if len(pcts) < 3 || pcts[0] != 0 || pcts[len(pcts)-2] != 100 || pcts[len(pcts)-1] != -1 {
		t.Fatalf("percent 序列 %v", pcts)
	}
	for i := 1; i < len(pcts)-1; i++ {
		if pcts[i] < pcts[i-1] {
			t.Fatalf("进度不应倒退: %v", pcts)
		}
	}
	if !strings.HasPrefix(texts[0], "下载中") {
		t.Fatalf("文字进度 %v", texts)
	}
	// 已下载过的包复用,不再出现进度条
	pcts = nil
	l.SetCurrent("1.0.0")
	os.RemoveAll(l.AppDir("1.1.0"))
	os.MkdirAll(filepath.Join(l.UpdatesDir(), "1.1.0"), 0o755)
	os.WriteFile(filepath.Join(l.UpdatesDir(), "1.1.0", "OpsAgent-app.zip"), f.files["OpsAgent-app.zip"], 0o644)
	if err := u.Apply(context.Background(), avail); err != nil {
		t.Fatal(err)
	}
	if len(pcts) != 0 {
		t.Fatalf("复用缓存不应上报下载进度: %v", pcts)
	}
}

func TestApplyRollsBackWhenNewVersionUnhealthy(t *testing.T) {
	l := installed(t, "1.0.0")
	f := newFixture(t, "1.1.0", nil)
	u, host := newUpdater(l, f, nil)
	avail, _ := Check(context.Background(), f, f.keys, "1.0.0")
	// 新版本启动期间改写了 state
	host.healthyAt = func(dir string) bool {
		if filepath.Base(dir) == "1.1.0" {
			os.WriteFile(filepath.Join(l.StateDir(), "triage-settings.json"), []byte("migrated"), 0o644)
			os.WriteFile(filepath.Join(l.StateDir(), "new-only.json"), []byte("x"), 0o644)
			return false
		}
		return true
	}
	err := u.Apply(context.Background(), avail)
	if err == nil || !strings.Contains(err.Error(), "已回退到 1.0.0") {
		t.Fatalf("应回退: %v", err)
	}
	if cur, _ := l.Current(); cur != "1.0.0" {
		t.Fatalf("current = %s", cur)
	}
	if strings.Join(host.log, ",") != "stop,start 1.1.0,stop,start 1.0.0" {
		t.Fatalf("host 调用顺序 %v", host.log)
	}
	if got, _ := os.ReadFile(filepath.Join(l.StateDir(), "triage-settings.json")); string(got) != "old" {
		t.Fatalf("state 应还原: %q", got)
	}
	if _, err := os.Stat(filepath.Join(l.StateDir(), "new-only.json")); !os.IsNotExist(err) {
		t.Fatal("新版本写下的 state 文件应清掉")
	}
	if got, _ := os.ReadFile(filepath.Join(l.StateDir(), "web_server.log")); string(got) != "log" {
		t.Fatal("日志不在快照范围,不应被动")
	}
	if _, err := os.Stat(filepath.Join(l.StateDir(), "graphs", "big.json")); err != nil {
		t.Fatal("快照排除的目录不应被删")
	}
	if _, err := os.Stat(l.AppDir("1.1.0")); !os.IsNotExist(err) {
		t.Fatal("失败的新版本目录应删掉")
	}
	if rec, _ := u.LoadRecord(); rec.Phase != PhaseFailed {
		t.Fatalf("record %+v", rec)
	}
}

func TestApplyRejectsCorruptDownloadWithoutSwitching(t *testing.T) {
	l := installed(t, "1.0.0")
	f := newFixture(t, "1.1.0", nil)
	u, host := newUpdater(l, f, func(string) bool { return true })
	avail, _ := Check(context.Background(), f, f.keys, "1.0.0")
	f.files["OpsAgent-app.zip"] = bytes.Repeat([]byte("x"), len(f.files["OpsAgent-app.zip"]))
	if err := u.Apply(context.Background(), avail); ErrorKind(err) != "bad_release" {
		t.Fatalf("哈希不符应拒绝: %v", err)
	}
	if cur, _ := l.Current(); cur != "1.0.0" || len(host.log) != 0 {
		t.Fatalf("不应停中台或切换: %s %v", cur, host.log)
	}
	if entries, _ := os.ReadDir(filepath.Join(l.UpdatesDir(), "1.1.0")); len(entries) != 0 {
		t.Fatalf("坏下载应删掉: %v", entries)
	}
}

func TestApplyRejectsPackageVersionMismatch(t *testing.T) {
	l := installed(t, "1.0.0")
	f := newFixture(t, "1.1.0", nil)
	f.files["OpsAgent-app.zip"] = appZip(t, "1.2.0", nil)
	f = refresh(t, f)
	u, host := newUpdater(l, f, func(string) bool { return true })
	avail, _ := Check(context.Background(), f, f.keys, "1.0.0")
	if err := u.Apply(context.Background(), avail); err == nil || !strings.Contains(err.Error(), "不一致") {
		t.Fatalf("包内版本不符应拒绝: %v", err)
	}
	if len(host.log) != 0 {
		t.Fatal("不应停中台")
	}
}

func TestExtractRejectsZipSlip(t *testing.T) {
	l := installed(t, "1.0.0")
	f := newFixture(t, "1.1.0", nil)
	f.files["OpsAgent-app.zip"] = appZip(t, "1.1.0", map[string]string{"../../evil.txt": "x"})
	f = refresh(t, f)
	u, _ := newUpdater(l, f, func(string) bool { return true })
	avail, _ := Check(context.Background(), f, f.keys, "1.0.0")
	if err := u.Apply(context.Background(), avail); err == nil || !strings.Contains(err.Error(), "非法路径") {
		t.Fatalf("应拒绝越界路径: %v", err)
	}
	if _, err := os.Stat(filepath.Join(l.Root, "..", "evil.txt")); !os.IsNotExist(err) {
		t.Fatal("不应写到安装目录外")
	}
}

// refresh 改了资产内容后重签 manifest。
func refresh(t *testing.T, f *fixture) *fixture {
	var m release.Manifest
	json.Unmarshal(f.files[release.ManifestName], &m)
	for i, a := range m.Assets {
		m.Assets[i].Size, m.Assets[i].SHA256 = int64(len(f.files[a.Name])), sum(f.files[a.Name])
	}
	data, _ := json.Marshal(m)
	f.files[release.ManifestName] = data
	f.files[release.SignatureName] = []byte(release.Sign(f.priv, data))
	for i, a := range f.rel.Assets {
		f.rel.Assets[i].Size = int64(len(f.files[a.Name]))
	}
	return f
}

func TestRecoverRollsBackInterruptedSwitch(t *testing.T) {
	l := installed(t, "1.0.0")
	f := newFixture(t, "1.1.0", nil)
	u, host := newUpdater(l, f, func(string) bool { return false })
	avail, _ := Check(context.Background(), f, f.keys, "1.0.0")
	// 模拟:切换完成、健康检查途中外壳崩溃
	ra, _ := f.rel.Asset("OpsAgent-app.zip")
	archive, err := u.fetch(context.Background(), "1.1.0", mustAsset(avail.Manifest, release.KindApp), ra)
	if err != nil {
		t.Fatal(err)
	}
	if err := u.stage(archive, "1.1.0"); err != nil {
		t.Fatal(err)
	}
	snap, _ := u.snapshotState("1.0.0")
	l.SetCurrent("1.1.0")
	u.save(&Record{From: "1.0.0", To: "1.1.0", Phase: PhaseVerifying, Snapshot: snap})
	os.WriteFile(filepath.Join(l.StateDir(), "triage-settings.json"), []byte("migrated"), 0o644)

	if err := u.Recover(context.Background()); err != nil {
		t.Fatal(err)
	}
	if cur, _ := l.Current(); cur != "1.0.0" {
		t.Fatalf("current = %s", cur)
	}
	if got, _ := os.ReadFile(filepath.Join(l.StateDir(), "triage-settings.json")); string(got) != "old" {
		t.Fatal("state 应还原")
	}
	if strings.Join(host.log, ",") != "stop" || filepath.Base(host.appDir) != "1.0.0" {
		t.Fatalf("Recover 只停中台并指回旧版,由外壳随后拉起: %v %s", host.log, host.appDir)
	}
	rec, _ := u.LoadRecord()
	if rec.Phase != PhaseFailed || !strings.Contains(rec.Error, "已回退") {
		t.Fatalf("record %+v", rec)
	}
	if err := u.Recover(context.Background()); err != nil || len(host.log) != 1 {
		t.Fatal("终态记录再次 Recover 应什么都不做")
	}
}

func TestRecoverMarksInterruptedDownloadFailed(t *testing.T) {
	l := installed(t, "1.0.0")
	u, host := newUpdater(l, newFixture(t, "1.1.0", nil), func(string) bool { return true })
	u.save(&Record{From: "1.0.0", To: "1.1.0", Phase: PhaseDownloading})
	if err := u.Recover(context.Background()); err != nil {
		t.Fatal(err)
	}
	if rec, _ := u.LoadRecord(); rec.Phase != PhaseFailed || len(host.log) != 0 {
		t.Fatalf("record %+v host %v", rec, host.log)
	}
}

func mustAsset(m *release.Manifest, kind string) release.Asset {
	a, _ := m.Asset(kind)
	return a
}

func TestNeedsShellAndFetchShell(t *testing.T) {
	l := installed(t, "1.0.0")
	f := newFixture(t, "1.1.0", nil)
	u, _ := newUpdater(l, f, nil)
	avail, _ := Check(context.Background(), f, f.keys, "1.0.0")
	exe := filepath.Join(t.TempDir(), "OpsAgent.exe")
	os.WriteFile(exe, []byte("new shell exe 1.1.0"), 0o755)
	if NeedsShell(avail.Manifest, exe) {
		t.Fatal("哈希相同不应换外壳")
	}
	os.WriteFile(exe, []byte("old shell"), 0o755)
	if !NeedsShell(avail.Manifest, exe) {
		t.Fatal("哈希不同应换外壳")
	}
	path, err := u.FetchShell(context.Background(), avail)
	if err != nil {
		t.Fatal(err)
	}
	if got, _ := os.ReadFile(path); string(got) != "new shell exe 1.1.0" {
		t.Fatal("下载内容不对")
	}
	noShell := newFixture(t, "1.2.0", func(m *release.Manifest) { m.ShellSHA256 = "" })
	if NeedsShell(mustCheck(t, noShell).Manifest, exe) {
		t.Fatal("manifest 没给外壳不应换")
	}
}

func mustCheck(t *testing.T, f *fixture) *Available {
	avail, err := Check(context.Background(), f, f.keys, "0.0.1")
	if err != nil {
		t.Fatal(err)
	}
	return avail
}

// ---- GitHub 客户端 ----

func TestClientAgainstFakeGitHub(t *testing.T) {
	var assetAuth []string
	storage := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		assetAuth = append(assetAuth, r.Header.Get("Authorization"))
		io.WriteString(w, "payload")
	}))
	defer storage.Close()
	// 存储地址换成 localhost,与 API 的 127.0.0.1 不同主机
	storageURL := strings.Replace(storage.URL, "127.0.0.1", "localhost", 1)
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer good" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		switch r.URL.Path {
		case "/repos/o/r/releases/latest":
			io.WriteString(w, `{"tag_name":"v1.0.0","assets":[{"id":7,"name":"a.zip","size":7}]}`)
		case "/repos/o/r/releases/assets/7":
			if r.Header.Get("Accept") != "application/octet-stream" {
				w.WriteHeader(http.StatusBadRequest)
				return
			}
			http.Redirect(w, r, storageURL+"/signed?x=1", http.StatusFound)
		case "/repos/o/limited/releases/latest":
			w.Header().Set("X-RateLimit-Remaining", "0")
			w.WriteHeader(http.StatusForbidden)
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer api.Close()

	client := func(repo, token string) *Client {
		c, err := NewClient(Config{Repo: repo, Token: token})
		if err != nil {
			t.Fatal(err)
		}
		c.API = api.URL
		return c
	}
	ctx := context.Background()
	rel, err := client("o/r", "good").Latest(ctx)
	if err != nil || rel.TagName != "v1.0.0" {
		t.Fatalf("Latest = %+v %v", rel, err)
	}
	var buf bytes.Buffer
	if err := client("o/r", "good").Download(ctx, rel.Assets[0], &buf); err != nil || buf.String() != "payload" {
		t.Fatalf("Download = %q %v", buf.String(), err)
	}
	if len(assetAuth) != 1 || assetAuth[0] != "" {
		t.Fatalf("跳转到存储主机不应带 token: %q", assetAuth)
	}
	if err := client("o/r", "good").Download(ctx, ReleaseAsset{ID: 7, Name: "a.zip", Size: 3}, io.Discard); ErrorKind(err) != "bad_release" {
		t.Fatalf("大小不符应失败: %v", err)
	}
	for repo, token := range map[string]string{"o/r": "bad"} {
		if _, err := client(repo, token).Latest(ctx); ErrorKind(err) != "unauthorized" {
			t.Fatalf("坏 token 应提示失效: %v", err)
		}
	}
	if _, err := client("o/missing", "good").Latest(ctx); ErrorKind(err) != "not_found" {
		t.Fatalf("404 分类: %v", err)
	}
	if _, err := client("o/limited", "good").Latest(ctx); ErrorKind(err) != "rate_limited" {
		t.Fatalf("限流分类: %v", err)
	}
	api.Close()
	if _, err := client("o/r", "good").Latest(ctx); ErrorKind(err) != "network" {
		t.Fatalf("网络不可达分类: %v", err)
	}
	if _, err := NewClient(Config{}); ErrorKind(err) != "not_configured" {
		t.Fatalf("未配置分类: %v", err)
	}
}

func TestConfigRoundTrip(t *testing.T) {
	path := filepath.Join(t.TempDir(), "config", ConfigName)
	if c, err := LoadConfig(path); err != nil || c.Repo != "" {
		t.Fatalf("缺文件应视为未配置: %+v %v", c, err)
	}
	if err := SaveConfig(path, Config{Repo: "bad repo"}); err == nil {
		t.Fatal("应拒绝非法仓名")
	}
	if err := SaveConfig(path, Config{Repo: "o/r", Token: "t", Proxy: "http://127.0.0.1:7897"}); err != nil {
		t.Fatal(err)
	}
	info, _ := os.Stat(path)
	if runtime.GOOS != "windows" && info.Mode().Perm() != 0o600 { // Windows 只有只读位,权限靠用户目录 ACL
		t.Fatalf("token 文件权限 %v", info.Mode().Perm())
	}
	if c, _ := LoadConfig(path); c != (Config{Repo: "o/r", Token: "t", Proxy: "http://127.0.0.1:7897"}) {
		t.Fatalf("读回 %+v", c)
	}
}
