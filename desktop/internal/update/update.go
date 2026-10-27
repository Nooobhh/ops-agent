package update

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/Nooobhh/ops-agent/desktop/internal/apphost"
	"github.com/Nooobhh/ops-agent/desktop/internal/release"
)

// Source 抽象 GitHub 客户端,测试换成假实现。
type Source interface {
	Latest(ctx context.Context) (*Release, error)
	Download(ctx context.Context, asset ReleaseAsset, w io.Writer) error
}

// Available 已验签、比当前新的版本。
type Available struct {
	Manifest *release.Manifest
	Release  *Release
}

// Check 取最新 release → 下载并验签 manifest → 比版本;没有更新返回 nil。
func Check(ctx context.Context, src Source, keys []ed25519.PublicKey, current string) (*Available, error) {
	rel, err := src.Latest(ctx)
	if err != nil {
		return nil, err
	}
	small := func(name string) ([]byte, error) {
		asset, ok := rel.Asset(name)
		if !ok || asset.Size > 1<<20 {
			return nil, errorf("bad_release", "release %s 缺少 %s", rel.TagName, name)
		}
		var buf bytes.Buffer
		ctx, cancel := context.WithTimeout(ctx, time.Minute)
		defer cancel()
		err := src.Download(ctx, asset, &buf)
		return buf.Bytes(), err
	}
	data, err := small(release.ManifestName)
	if err != nil {
		return nil, err
	}
	sig, err := small(release.SignatureName)
	if err != nil {
		return nil, err
	}
	if err := release.Verify(keys, data, sig); err != nil {
		return nil, errorf("bad_release", "%v", err)
	}
	m, err := release.ParseManifest(data)
	if err != nil {
		return nil, errorf("bad_release", "%v", err)
	}
	if rel.TagName != "v"+m.Version {
		return nil, errorf("bad_release", "release 标签 %s 与 manifest 版本 %s 不一致", rel.TagName, m.Version)
	}
	if !release.Newer(m.Version, current) {
		return nil, nil
	}
	app, ok := m.Asset(release.KindApp)
	if !ok {
		return nil, errorf("bad_release", "manifest 缺少应用包")
	}
	if ra, ok := rel.Asset(app.Name); !ok || ra.Size != app.Size {
		return nil, errorf("bad_release", "release 缺少 %s 或大小与 manifest 不符", app.Name)
	}
	return &Available{Manifest: m, Release: rel}, nil
}

// Host 中台进程控制(apphost.Server 实现)。
type Host interface {
	Stop()
	SetAppDir(dir string)
	Start() error
	WaitHealthy(ctx context.Context, timeout time.Duration) bool
}

const (
	StateName = "update-state.json"

	PhaseDownloading = "downloading"
	PhaseStaged      = "staged"
	PhaseSwitching   = "switching"
	PhaseVerifying   = "verifying"
	PhaseRollingBack = "rolling_back"
	PhaseDone        = "done"
	PhaseFailed      = "failed"
)

// Record 落 shell\update-state.json:外壳在任一步崩溃,重启后据此续完或回退。
type Record struct {
	From      string `json:"from"`
	To        string `json:"to"`
	Phase     string `json:"phase"`
	Snapshot  string `json:"snapshot,omitempty"`
	Error     string `json:"error,omitempty"`
	UpdatedAt string `json:"updated_at"`
}

type Updater struct {
	Layout        apphost.Layout
	Host          Host
	Source        Source
	HealthTimeout time.Duration
	// Progress 给面板显示当前步骤;Percent 给面板进度条,下载中 0~100、不在下载时 -1;都可为 nil
	Progress func(text string)
	Percent  func(pct int)
	Logf     func(format string, args ...any)
}

func (u *Updater) statePath() string { return filepath.Join(u.Layout.ShellDir(), StateName) }

func (u *Updater) LoadRecord() (Record, error) {
	var r Record
	data, err := os.ReadFile(u.statePath())
	if errors.Is(err, os.ErrNotExist) {
		return r, nil
	}
	if err != nil {
		return r, err
	}
	return r, json.Unmarshal(data, &r)
}

func (u *Updater) save(r *Record) {
	r.UpdatedAt = time.Now().Format(time.RFC3339)
	data, _ := json.MarshalIndent(r, "", "  ")
	tmp := u.statePath() + ".tmp"
	err := os.WriteFile(tmp, data, 0o644)
	if err == nil {
		err = os.Rename(tmp, u.statePath())
	}
	if err != nil {
		u.logf("写 %s 失败: %v", StateName, err)
	}
	u.logf("更新状态 %s → %s: %s %s", r.From, r.To, r.Phase, r.Error)
}

func (u *Updater) logf(format string, args ...any) {
	if u.Logf != nil {
		u.Logf(format, args...)
	}
}

func (u *Updater) progress(text string) {
	if u.Progress != nil {
		u.Progress(text)
	}
}

func (u *Updater) percent(pct int) {
	if u.Percent != nil {
		u.Percent(pct)
	}
}

// Apply 下载 → 校验 → 解压 → 停中台 → 快照 state → 切 current → 起中台 → 健康检查;失败回退旧版。
func (u *Updater) Apply(ctx context.Context, avail *Available) error {
	from, err := u.Layout.Current()
	if err != nil {
		return err
	}
	m := avail.Manifest
	to := m.Version
	if err := apphost.ValidVersionName(to); err != nil || !release.Newer(to, from) {
		return fmt.Errorf("%s 不比当前 %s 新", to, from)
	}
	rec := &Record{From: from, To: to, Phase: PhaseDownloading}
	u.save(rec)
	fail := func(err error) error {
		rec.Phase, rec.Error = PhaseFailed, err.Error()
		u.save(rec)
		return err
	}

	asset, _ := m.Asset(release.KindApp)
	ra, _ := avail.Release.Asset(asset.Name)
	archive, err := u.fetch(ctx, to, asset, ra)
	if err != nil {
		return fail(err)
	}
	u.progress("解压新版本")
	if err := u.stage(archive, to); err != nil {
		return fail(err)
	}
	rec.Phase = PhaseStaged
	u.save(rec)

	u.progress("停止中台")
	rec.Phase = PhaseSwitching
	u.save(rec)
	u.Host.Stop()
	snapshot, err := u.snapshotState(from)
	if err != nil {
		u.Host.Start()
		return fail(fmt.Errorf("备份 state 失败,未切换: %w", err))
	}
	rec.Snapshot = snapshot
	if err := u.Layout.SetCurrent(to); err != nil {
		u.Host.Start()
		return fail(err)
	}
	u.Host.SetAppDir(u.Layout.AppDir(to))
	rec.Phase = PhaseVerifying
	u.save(rec)
	u.progress("启动新版本并检查健康")
	if err := u.Host.Start(); err == nil && u.Host.WaitHealthy(ctx, u.HealthTimeout) {
		rec.Phase = PhaseDone
		u.save(rec)
		u.cleanup(from, to)
		return nil
	}
	reason := fmt.Errorf("新版本 %s 启动失败或 %d 秒内未就绪,已回退到 %s;详见 data\\state\\web_server.log",
		to, int(u.HealthTimeout.Seconds()), from)
	u.rollback(ctx, rec)
	if err := u.Host.Start(); err != nil || !u.Host.WaitHealthy(ctx, u.HealthTimeout) {
		u.logf("回退后旧版本 %s 也未就绪", from)
	}
	return fail(reason)
}

// Recover 外壳启动时、拉起中台之前调用:上次更新停在半路就收尾。
func (u *Updater) Recover(ctx context.Context) error {
	rec, err := u.LoadRecord()
	if err != nil || rec.From == "" {
		return err
	}
	switch rec.Phase {
	case PhaseDownloading, PhaseStaged:
		rec.Phase, rec.Error = PhaseFailed, "外壳在更新途中退出,未切换版本"
		u.save(&rec)
		if cur, _ := u.Layout.Current(); cur != rec.From {
			return u.Layout.SetCurrent(rec.From)
		}
	case PhaseSwitching, PhaseVerifying, PhaseRollingBack:
		// 新版本是否健康未被确认:保守回退
		u.rollback(ctx, &rec)
		rec.Phase, rec.Error = PhaseFailed, fmt.Sprintf("外壳在更新途中退出,已回退到 %s", rec.From)
		u.save(&rec)
	}
	return nil
}

func (u *Updater) rollback(ctx context.Context, rec *Record) {
	rec.Phase = PhaseRollingBack
	u.save(rec)
	u.progress("回退到 " + rec.From)
	u.Host.Stop()
	if err := u.Layout.SetCurrent(rec.From); err != nil {
		u.logf("回退写 current 失败: %v", err)
	}
	if rec.Snapshot != "" {
		if err := restoreState(rec.Snapshot, u.Layout.StateDir()); err != nil {
			u.logf("还原 state 快照失败: %v", err)
		}
	}
	u.Host.SetAppDir(u.Layout.AppDir(rec.From))
	if rec.To != rec.From {
		os.RemoveAll(u.Layout.AppDir(rec.To))
	}
}

// fetch 下载到 shell\updates\<ver>\,已有且校验通过就复用。
func (u *Updater) fetch(ctx context.Context, version string, asset release.Asset, ra ReleaseAsset) (string, error) {
	dir := filepath.Join(u.Layout.UpdatesDir(), version)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", err
	}
	path := filepath.Join(dir, asset.Name)
	if checkFile(path, asset.Size, asset.SHA256) == nil {
		return path, nil
	}
	part := path + ".part"
	f, err := os.Create(part)
	if err != nil {
		return "", err
	}
	w := &progressWriter{total: asset.Size, last: -1, report: u.progress, percent: u.percent}
	u.percent(0)
	defer u.percent(-1)
	err = u.Source.Download(ctx, ra, io.MultiWriter(f, w))
	f.Close()
	if err == nil {
		err = checkFile(part, asset.Size, asset.SHA256)
	}
	if err == nil {
		err = os.Rename(part, path)
	}
	if err != nil {
		os.Remove(part)
		return "", err
	}
	return path, nil
}

func checkFile(path string, size int64, wantSHA string) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	h := sha256.New()
	n, err := io.Copy(h, f)
	if err != nil {
		return err
	}
	if n != size || hex.EncodeToString(h.Sum(nil)) != strings.ToLower(wantSHA) {
		return errorf("bad_release", "%s 校验失败(大小或 sha256 与 manifest 不符)", filepath.Base(path))
	}
	return nil
}

type progressWriter struct {
	total, done int64
	last        int64
	report      func(string)
	percent     func(int)
}

func (p *progressWriter) Write(b []byte) (int, error) {
	p.done += int64(len(b))
	if pct := min(p.done*100/max(p.total, 1), 100); pct != p.last {
		p.last = pct
		p.report(fmt.Sprintf("下载中 %d%%", pct))
		p.percent(int(pct))
	}
	return len(b), nil
}

// stage 解到 app\<ver>.tmp 再改名,半截解压不会冒充已安装版本。
func (u *Updater) stage(archive, version string) error {
	target := u.Layout.AppDir(version)
	if info, err := apphost.ReadPackageInfo(target); err == nil && info.Version == version {
		return nil
	}
	os.RemoveAll(target)
	tmp := target + ".tmp"
	os.RemoveAll(tmp)
	if err := extractZip(archive, tmp); err != nil {
		os.RemoveAll(tmp)
		return err
	}
	info, err := apphost.ReadPackageInfo(tmp)
	if err == nil && info.Version != version {
		err = fmt.Errorf("包内版本 %s 与 manifest %s 不一致", info.Version, version)
	}
	if err == nil {
		_, err = os.Stat(apphost.PythonPath(tmp))
	}
	if err != nil {
		os.RemoveAll(tmp)
		return fmt.Errorf("应用包不完整: %w", err)
	}
	return os.Rename(tmp, target)
}

func extractZip(archive, dest string) error {
	r, err := zip.OpenReader(archive)
	if err != nil {
		return err
	}
	defer r.Close()
	for _, f := range r.File {
		name := filepath.FromSlash(f.Name)
		if !filepath.IsLocal(name) {
			return fmt.Errorf("压缩包含非法路径 %q", f.Name)
		}
		path := filepath.Join(dest, name)
		if f.FileInfo().IsDir() {
			if err := os.MkdirAll(path, 0o755); err != nil {
				return err
			}
			continue
		}
		if !f.Mode().IsRegular() {
			return fmt.Errorf("压缩包含非普通文件 %q", f.Name)
		}
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			return err
		}
		if err := writeEntry(f, path); err != nil {
			return err
		}
	}
	return nil
}

func writeEntry(f *zip.File, path string) error {
	src, err := f.Open()
	if err != nil {
		return err
	}
	defer src.Close()
	dst, err := os.OpenFile(path, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, f.Mode().Perm()|0o200)
	if err != nil {
		return err
	}
	_, err = io.Copy(dst, src)
	if cerr := dst.Close(); err == nil {
		err = cerr
	}
	return err
}

// 快照不含日志与可再生的大目录;records/knowledge 只追加且格式向后兼容,不快照。
var snapshotSkipDirs = map[string]bool{"graphs": true, "kb-import": true}

func skipInSnapshot(rel string, isDir bool) bool {
	first := strings.Split(filepath.ToSlash(rel), "/")[0]
	if snapshotSkipDirs[first] {
		return true
	}
	base := filepath.Base(rel)
	return !isDir && (strings.HasSuffix(base, ".log") || strings.Contains(base, ".log.") ||
		strings.HasSuffix(base, ".lock") || base == "web_server.stop")
}

func (u *Updater) snapshotState(from string) (string, error) {
	path := filepath.Join(u.Layout.UpdatesDir(), "state-"+from+".zip")
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return "", err
	}
	tmp := path + ".tmp"
	out, err := os.Create(tmp)
	if err != nil {
		return "", err
	}
	zw := zip.NewWriter(out)
	root := u.Layout.StateDir()
	err = filepath.WalkDir(root, func(p string, d os.DirEntry, err error) error {
		if err != nil || p == root {
			return err
		}
		rel, _ := filepath.Rel(root, p)
		if skipInSnapshot(rel, d.IsDir()) {
			if d.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		if d.IsDir() || !d.Type().IsRegular() {
			return nil
		}
		w, err := zw.Create(filepath.ToSlash(rel))
		if err != nil {
			return err
		}
		src, err := os.Open(p)
		if err != nil {
			return err
		}
		defer src.Close()
		_, err = io.Copy(w, src)
		return err
	})
	if cerr := zw.Close(); err == nil {
		err = cerr
	}
	if cerr := out.Close(); err == nil {
		err = cerr
	}
	if err == nil {
		err = os.Rename(tmp, path)
	}
	if err != nil {
		os.Remove(tmp)
		return "", err
	}
	return path, nil
}

// restoreState 删掉快照覆盖范围内新版本写下的文件,再解回快照。
func restoreState(snapshot, stateDir string) error {
	if _, err := os.Stat(snapshot); err != nil {
		return err
	}
	entries, err := os.ReadDir(stateDir)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	for _, e := range entries {
		if !skipInSnapshot(e.Name(), e.IsDir()) {
			os.RemoveAll(filepath.Join(stateDir, e.Name()))
		}
	}
	return extractZip(snapshot, stateDir)
}

// cleanup 保留当前与上一版;更早的版本、本次下载包与更早的 state 快照删掉。
func (u *Updater) cleanup(from, to string) {
	versions, _ := u.Layout.Versions()
	for _, v := range versions {
		if v != from && v != to {
			os.RemoveAll(u.Layout.AppDir(v))
		}
	}
	entries, _ := os.ReadDir(u.Layout.UpdatesDir())
	for _, e := range entries {
		if e.Name() != "state-"+from+".zip" {
			os.RemoveAll(filepath.Join(u.Layout.UpdatesDir(), e.Name()))
		}
	}
}

// ---- 外壳自身 ----

const ShellDownloadDir = "shell"

func FileSHA256(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// NeedsShell 新外壳与运行中的 exe 哈希不同才换;manifest 没给外壳则不换。
func NeedsShell(m *release.Manifest, exe string) bool {
	if m.ShellSHA256 == "" {
		return false
	}
	if _, ok := m.Asset(release.KindShell); !ok {
		return false
	}
	sum, err := FileSHA256(exe)
	return err == nil && !strings.EqualFold(sum, m.ShellSHA256)
}

// FetchShell 下载新外壳到 shell\updates\shell\,返回校验过的路径。
func (u *Updater) FetchShell(ctx context.Context, avail *Available) (string, error) {
	asset, _ := avail.Manifest.Asset(release.KindShell)
	ra, ok := avail.Release.Asset(asset.Name)
	if !ok {
		return "", errorf("bad_release", "release 缺少 %s", asset.Name)
	}
	if !strings.EqualFold(asset.SHA256, avail.Manifest.ShellSHA256) {
		return "", errorf("bad_release", "manifest 外壳哈希前后不一致")
	}
	return u.fetch(ctx, ShellDownloadDir, asset, ra)
}
