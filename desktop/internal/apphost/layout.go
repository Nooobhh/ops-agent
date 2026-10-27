// Package apphost 安装目录布局与中台进程托管,平台无关部分在 mac 上可测。
package apphost

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
)

// Layout 安装根目录:app\<版本>\ 只读(更新整体替换)、data\ 是 Python 中台的数据根
// (OPS_AGENT_DATA_ROOT,更新永不触碰)、shell\ 归外壳(日志、WebView2 数据、更新缓存)。
type Layout struct{ Root string }

const PackageInfoName = "package-info.json"

func (l Layout) AppRoot() string              { return filepath.Join(l.Root, "app") }
func (l Layout) AppDir(version string) string { return filepath.Join(l.AppRoot(), version) }
func (l Layout) DataDir() string              { return filepath.Join(l.Root, "data") }
func (l Layout) StateDir() string             { return filepath.Join(l.DataDir(), "state") }
func (l Layout) ConfigDir() string            { return filepath.Join(l.DataDir(), "config") }
func (l Layout) ShellDir() string             { return filepath.Join(l.Root, "shell") }
func (l Layout) UpdatesDir() string           { return filepath.Join(l.ShellDir(), "updates") }
func (l Layout) currentFile() string          { return filepath.Join(l.AppRoot(), "current") }

// EnsureDirs 首启建外壳与数据根骨架。knowledge\ 与 env\ 不建:是使用者放进来的应用数据,包内不带。
func (l Layout) EnsureDirs() error {
	for _, dir := range []string{l.ShellDir(), l.ConfigDir(), l.StateDir()} {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return err
		}
	}
	return nil
}

// Current 读 app\current 指向的版本,并确认该版本目录完整。
func (l Layout) Current() (string, error) {
	data, err := os.ReadFile(l.currentFile())
	if err != nil {
		return "", fmt.Errorf("找不到 %s,安装目录不完整", l.currentFile())
	}
	version := strings.TrimSpace(strings.TrimPrefix(string(data), "\xef\xbb\xbf"))
	if err := ValidVersionName(version); err != nil {
		return "", fmt.Errorf("%s: %w", l.currentFile(), err)
	}
	if _, err := os.Stat(PythonPath(l.AppDir(version))); err != nil {
		return "", fmt.Errorf("应用版本 %q 不完整:缺少 Python 运行时", version)
	}
	return version, nil
}

// SetCurrent 先写临时文件再改名,切换版本是原子的(Windows 上 os.Rename 覆盖已有文件)。
func (l Layout) SetCurrent(version string) error {
	if err := ValidVersionName(version); err != nil {
		return err
	}
	tmp := l.currentFile() + ".tmp"
	if err := os.WriteFile(tmp, []byte(version+"\n"), 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, l.currentFile())
}

// Versions 列出已解压完整(有 package-info.json)的版本目录名。
func (l Layout) Versions() ([]string, error) {
	entries, err := os.ReadDir(l.AppRoot())
	if err != nil {
		return nil, err
	}
	var out []string
	for _, e := range entries {
		if !e.IsDir() || ValidVersionName(e.Name()) != nil {
			continue
		}
		if _, err := os.Stat(filepath.Join(l.AppRoot(), e.Name(), PackageInfoName)); err == nil {
			out = append(out, e.Name())
		}
	}
	sort.Strings(out)
	return out, nil
}

// ValidVersionName 版本号会拼进路径:只认单层目录名,临时目录(含 .tmp)不算。
func ValidVersionName(v string) error {
	if v == "" || v == "." || v == ".." || strings.ContainsAny(v, `/\:`) || strings.HasSuffix(v, ".tmp") ||
		strings.HasPrefix(v, ".") {
		return fmt.Errorf("非法版本目录名 %q", v)
	}
	return nil
}

// PythonPath 应用目录自带的 Python;Windows 用 python.exe(配 CREATE_NO_WINDOW,worker 继承隐藏控制台)。
func PythonPath(appDir string) string {
	if runtime.GOOS == "windows" {
		return filepath.Join(appDir, "runtime", "python.exe")
	}
	return filepath.Join(appDir, "runtime", "bin", "python3")
}

type PackageInfo struct {
	Version string `json:"version"`
	Commit  string `json:"commit"`
}

func ReadPackageInfo(appDir string) (PackageInfo, error) {
	var info PackageInfo
	data, err := os.ReadFile(filepath.Join(appDir, PackageInfoName))
	if err != nil {
		return info, err
	}
	if err := json.Unmarshal(data, &info); err != nil {
		return info, err
	}
	if info.Version == "" {
		return info, errors.New(PackageInfoName + " 缺少 version")
	}
	return info, nil
}
