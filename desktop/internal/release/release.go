// Package release 发布清单(manifest)、ed25519 签名与 SemVer 比较;签名工具与外壳共用这一份代码。
package release

import (
	"bufio"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
)

const (
	ManifestName  = "manifest.json"
	SignatureName = "manifest.json.sig"

	KindApp     = "app"     // 更新用:app\<ver>\ 目录内容
	KindShell   = "shell"   // 外壳 exe
	KindInstall = "install" // 首装 zip
	KindLinux   = "linux"   // Linux tarball

	pubPrefix  = "ed25519 "
	privPrefix = "ed25519-private "
)

type Asset struct {
	Name   string `json:"name"`
	Kind   string `json:"kind"`
	Size   int64  `json:"size"`
	SHA256 string `json:"sha256"`
}

type Manifest struct {
	Version     string  `json:"version"`
	ReleasedAt  string  `json:"released_at"`
	Notes       string  `json:"notes"`
	ShellSHA256 string  `json:"shell_sha256"`
	Assets      []Asset `json:"assets"`
}

func ParseManifest(data []byte) (*Manifest, error) {
	var m Manifest
	if err := json.Unmarshal(data, &m); err != nil {
		return nil, fmt.Errorf("manifest 解析失败: %w", err)
	}
	if _, err := ParseVersion(m.Version); err != nil {
		return nil, err
	}
	for _, a := range m.Assets {
		if a.Name == "" || strings.ContainsAny(a.Name, `/\`) || len(a.SHA256) != 64 || a.Size <= 0 {
			return nil, fmt.Errorf("manifest 资产条目不合法: %+v", a)
		}
	}
	return &m, nil
}

// Asset 取某类资产;每类至多一个。
func (m *Manifest) Asset(kind string) (Asset, bool) {
	for _, a := range m.Assets {
		if a.Kind == kind {
			return a, true
		}
	}
	return Asset{}, false
}

// ---- 签名 ----

// ParsePublicKeys 每行 `ed25519 <base64>`,允许空行与 # 注释;多行 = 多把可信公钥(换钥时新旧并存一版)。
func ParsePublicKeys(text string) ([]ed25519.PublicKey, error) {
	var keys []ed25519.PublicKey
	scanner := bufio.NewScanner(strings.NewReader(text))
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		fields := strings.Fields(strings.TrimPrefix(line, pubPrefix))
		raw, err := base64.StdEncoding.DecodeString(fields[0])
		if !strings.HasPrefix(line, pubPrefix) || err != nil || len(raw) != ed25519.PublicKeySize {
			return nil, fmt.Errorf("公钥行不合法: %q", line)
		}
		keys = append(keys, ed25519.PublicKey(raw))
	}
	if len(keys) == 0 {
		return nil, errors.New("没有可信公钥")
	}
	return keys, nil
}

func ParsePrivateKey(text string) (ed25519.PrivateKey, error) {
	line := strings.TrimSpace(text)
	raw, err := base64.StdEncoding.DecodeString(strings.TrimPrefix(line, privPrefix))
	if !strings.HasPrefix(line, privPrefix) || err != nil || len(raw) != ed25519.SeedSize {
		return nil, errors.New("私钥文件格式不对(应为 `ed25519-private <base64 种子>`)")
	}
	return ed25519.NewKeyFromSeed(raw), nil
}

// GenerateKey 返回公钥行与私钥文件内容。
func GenerateKey() (pubLine, privText string, err error) {
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return "", "", err
	}
	return pubPrefix + base64.StdEncoding.EncodeToString(pub),
		privPrefix + base64.StdEncoding.EncodeToString(priv.Seed()) + "\n", nil
}

func PublicLine(priv ed25519.PrivateKey) string {
	return pubPrefix + base64.StdEncoding.EncodeToString(priv.Public().(ed25519.PublicKey))
}

// Sign 对原始字节签名,返回 base64 一行。
func Sign(priv ed25519.PrivateKey, data []byte) string {
	return base64.StdEncoding.EncodeToString(ed25519.Sign(priv, data)) + "\n"
}

// Verify 任一可信公钥验过即可。
func Verify(keys []ed25519.PublicKey, data []byte, sig []byte) error {
	raw, err := base64.StdEncoding.DecodeString(strings.TrimSpace(string(sig)))
	if err != nil || len(raw) != ed25519.SignatureSize {
		return errors.New("签名格式不对")
	}
	for _, key := range keys {
		if ed25519.Verify(key, data, raw) {
			return nil
		}
	}
	return errors.New("签名校验失败:manifest 不是可信密钥签发的,或已被篡改")
}

// ---- SemVer 2.0.0 ----

type Version struct {
	Major, Minor, Patch int
	Pre                 []string
}

func ParseVersion(v string) (Version, error) {
	var out Version
	core, pre, hasPre := strings.Cut(strings.TrimPrefix(v, "v"), "-")
	core, _, _ = strings.Cut(core, "+") // 构建元数据不参与比较
	if hasPre {
		pre, _, _ = strings.Cut(pre, "+")
	}
	parts := strings.Split(core, ".")
	if len(parts) != 3 {
		return out, fmt.Errorf("版本号 %q 不是 MAJOR.MINOR.PATCH", v)
	}
	nums := []*int{&out.Major, &out.Minor, &out.Patch}
	for i, p := range parts {
		n, err := strconv.Atoi(p)
		if err != nil || n < 0 || (len(p) > 1 && p[0] == '0') {
			return out, fmt.Errorf("版本号 %q 不合法", v)
		}
		*nums[i] = n
	}
	if hasPre {
		out.Pre = strings.Split(pre, ".")
		for _, id := range out.Pre {
			if id == "" {
				return out, fmt.Errorf("版本号 %q 预发布段不合法", v)
			}
		}
	}
	return out, nil
}

// Compare a<b 返回 -1,相等 0,a>b 返回 1。
func Compare(a, b Version) int {
	for _, d := range []int{a.Major - b.Major, a.Minor - b.Minor, a.Patch - b.Patch} {
		if d != 0 {
			return sign(d)
		}
	}
	switch {
	case len(a.Pre) == 0 && len(b.Pre) == 0:
		return 0
	case len(a.Pre) == 0:
		return 1 // 正式版高于同号预发布
	case len(b.Pre) == 0:
		return -1
	}
	for i := 0; i < len(a.Pre) && i < len(b.Pre); i++ {
		x, y := a.Pre[i], b.Pre[i]
		nx, ex := strconv.Atoi(x)
		ny, ey := strconv.Atoi(y)
		switch {
		case ex == nil && ey == nil:
			if nx != ny {
				return sign(nx - ny)
			}
		case ex == nil:
			return -1 // 数字标识低于字母标识
		case ey == nil:
			return 1
		case x != y:
			return sign(strings.Compare(x, y))
		}
	}
	return sign(len(a.Pre) - len(b.Pre))
}

// Newer candidate 是否严格新于 current;解析失败一律视为不新(拒绝而非误装)。
func Newer(candidate, current string) bool {
	c, err1 := ParseVersion(candidate)
	cur, err2 := ParseVersion(current)
	return err1 == nil && err2 == nil && Compare(c, cur) > 0
}

func sign(d int) int {
	switch {
	case d < 0:
		return -1
	case d > 0:
		return 1
	}
	return 0
}
