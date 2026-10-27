// Package update 更新源配置、GitHub Release 客户端、检查与下载切换回退。
package update

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

const ConfigName = "update.local.json"

// Config 存 data\config\update.local.json;token 可选(公开仓不需要),只写不回显。
type Config struct {
	Repo  string `json:"repo"`
	Token string `json:"token,omitempty"`
	Proxy string `json:"proxy,omitempty"`
}

var repoPattern = regexp.MustCompile(`^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$`)

func (c Config) Validate() error {
	if !repoPattern.MatchString(c.Repo) {
		return fmt.Errorf("发布仓应为 owner/name 形式,当前 %q", c.Repo)
	}
	if c.Proxy != "" {
		if u, err := url.Parse(c.Proxy); err != nil || u.Host == "" {
			return fmt.Errorf("代理地址不合法: %q", c.Proxy)
		}
	}
	return nil
}

// LoadConfig 文件不存在返回零值(未配置),不算错误。
func LoadConfig(path string) (Config, error) {
	var c Config
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return c, nil
	}
	if err != nil {
		return c, err
	}
	if err := json.Unmarshal(data, &c); err != nil {
		return c, fmt.Errorf("%s 解析失败: %w", path, err)
	}
	return c, nil
}

func SaveConfig(path string, c Config) error {
	if err := c.Validate(); err != nil {
		return err
	}
	data, _ := json.MarshalIndent(c, "", "  ")
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, append(data, '\n'), 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// Error 更新检查/下载失败的分类;都不影响中台运行。
type Error struct {
	Kind string // not_configured / unauthorized / not_found / rate_limited / network / bad_release
	Msg  string
}

func (e *Error) Error() string { return e.Msg }

func errorf(kind, format string, args ...any) error {
	return &Error{Kind: kind, Msg: fmt.Sprintf(format, args...)}
}

func ErrorKind(err error) string {
	var e *Error
	if errors.As(err, &e) {
		return e.Kind
	}
	return ""
}

type ReleaseAsset struct {
	ID   int64  `json:"id"`
	Name string `json:"name"`
	Size int64  `json:"size"`
}

type Release struct {
	TagName string         `json:"tag_name"`
	Assets  []ReleaseAsset `json:"assets"`
}

func (r *Release) Asset(name string) (ReleaseAsset, bool) {
	for _, a := range r.Assets {
		if a.Name == name {
			return a, true
		}
	}
	return ReleaseAsset{}, false
}

type Client struct {
	API   string // 缺省 https://api.github.com;测试指向假 GitHub
	Repo  string
	Token string
	HTTP  *http.Client
}

// NewClient 代理只认配置项与环境变量(Go 缺省),不读 WinHTTP/IE 系统代理。
func NewClient(c Config) (*Client, error) {
	if c.Repo == "" {
		return nil, errorf("not_configured", "未配置更新源:在面板「更新设置」填发布仓(私有仓另填 token)")
	}
	if err := c.Validate(); err != nil {
		return nil, errorf("not_configured", "%v", err)
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	if c.Proxy != "" {
		u, _ := url.Parse(c.Proxy)
		transport.Proxy = http.ProxyURL(u)
	}
	client := &Client{API: "https://api.github.com", Repo: c.Repo, Token: c.Token}
	client.HTTP = &http.Client{Transport: transport, CheckRedirect: client.checkRedirect}
	return client, nil
}

// checkRedirect 资产下载会 302 到预签名的存储地址:跨主机绝不带 token。
func (c *Client) checkRedirect(req *http.Request, via []*http.Request) error {
	if len(via) >= 5 {
		return errors.New("跳转过多")
	}
	if req.URL.Host != via[0].URL.Host {
		req.Header.Del("Authorization")
	}
	return nil
}

func (c *Client) request(ctx context.Context, path, accept string) (*http.Response, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(c.API, "/")+path, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", accept)
	req.Header.Set("X-GitHub-Api-Version", "2022-11-28")
	req.Header.Set("User-Agent", "ops-agent-shell")
	if c.Token != "" {
		req.Header.Set("Authorization", "Bearer "+c.Token)
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, errorf("network", "连不上 GitHub: %v", err)
	}
	if resp.StatusCode == http.StatusOK {
		return resp, nil
	}
	resp.Body.Close()
	switch {
	case resp.StatusCode == http.StatusUnauthorized:
		return nil, errorf("unauthorized", "更新检查失败:token 失效,请在面板「更新设置」更换")
	case resp.StatusCode == http.StatusForbidden && resp.Header.Get("X-RateLimit-Remaining") == "0":
		return nil, errorf("rate_limited", "GitHub 接口限流,稍后自动重试")
	case resp.StatusCode == http.StatusForbidden:
		return nil, errorf("unauthorized", "更新检查失败:token 无权读取发布仓 %s", c.Repo)
	case resp.StatusCode == http.StatusNotFound:
		return nil, errorf("not_found", "发布仓 %s 不存在、不可见或还没有发布;私有仓需要 token", c.Repo)
	}
	return nil, errorf("network", "GitHub 返回 %d", resp.StatusCode)
}

func (c *Client) Latest(ctx context.Context) (*Release, error) {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	resp, err := c.request(ctx, "/repos/"+c.Repo+"/releases/latest", "application/vnd.github+json")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var r Release
	if err := json.NewDecoder(io.LimitReader(resp.Body, 4<<20)).Decode(&r); err != nil {
		return nil, errorf("bad_release", "release 信息解析失败: %v", err)
	}
	return &r, nil
}

// Download 按资产大小封顶写入;超出或不足都算失败。
func (c *Client) Download(ctx context.Context, asset ReleaseAsset, w io.Writer) error {
	resp, err := c.request(ctx, fmt.Sprintf("/repos/%s/releases/assets/%d", c.Repo, asset.ID), "application/octet-stream")
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	n, err := io.Copy(w, io.LimitReader(resp.Body, asset.Size+1))
	if err != nil {
		return errorf("network", "下载 %s 中断: %v", asset.Name, err)
	}
	if n != asset.Size {
		return errorf("bad_release", "%s 大小不符:应为 %d,实得 %d", asset.Name, asset.Size, n)
	}
	return nil
}
