package release

import (
	"strings"
	"testing"
)

func TestSignVerifyRoundTripAndTamper(t *testing.T) {
	pubLine, privText, err := GenerateKey()
	if err != nil {
		t.Fatal(err)
	}
	priv, err := ParsePrivateKey(privText)
	if err != nil {
		t.Fatal(err)
	}
	if PublicLine(priv) != pubLine {
		t.Fatal("私钥推出的公钥应与生成时一致")
	}
	otherPub, _, _ := GenerateKey()
	keys, err := ParsePublicKeys("# 正式发布公钥\n\n" + otherPub + " 旧钥\n" + pubLine + "\n")
	if err != nil || len(keys) != 2 {
		t.Fatalf("ParsePublicKeys: %v %d", err, len(keys))
	}
	data := []byte(`{"version":"1.0.0"}`)
	sig := Sign(priv, data)
	if err := Verify(keys, data, []byte(sig)); err != nil {
		t.Fatalf("多钥任一验过即可: %v", err)
	}
	for i := range data {
		tampered := append([]byte(nil), data...)
		tampered[i] ^= 1
		if Verify(keys, tampered, []byte(sig)) == nil {
			t.Fatalf("篡改第 %d 字节应验签失败", i)
		}
	}
	onlyOther, _ := ParsePublicKeys(otherPub)
	if Verify(onlyOther, data, []byte(sig)) == nil {
		t.Fatal("错公钥应验签失败")
	}
	if Verify(keys, data, []byte("not-base64!")) == nil {
		t.Fatal("坏签名应拒绝")
	}
}

func TestKeyParsingRejectsGarbage(t *testing.T) {
	for _, text := range []string{"", "# only comment", "ed25519 abc", "rsa AAAA", "ed25519"} {
		if _, err := ParsePublicKeys(text); err == nil {
			t.Fatalf("应拒绝公钥 %q", text)
		}
	}
	if _, err := ParsePrivateKey("ed25519 AAAA"); err == nil {
		t.Fatal("应拒绝私钥")
	}
}

func TestParseManifest(t *testing.T) {
	sha := strings.Repeat("a", 64)
	m, err := ParseManifest([]byte(`{"version":"1.0.0-m9.2","assets":[
		{"name":"OpsAgent-app-1.0.0-m9.2-win-x64.zip","kind":"app","size":10,"sha256":"` + sha + `"}]}`))
	if err != nil {
		t.Fatal(err)
	}
	if a, ok := m.Asset(KindApp); !ok || a.Size != 10 {
		t.Fatalf("Asset = %+v %v", a, ok)
	}
	if _, ok := m.Asset(KindShell); ok {
		t.Fatal("没有的资产类型应返回 false")
	}
	for _, bad := range []string{
		`{"version":"x"}`,
		`{"version":"1.0.0","assets":[{"name":"../evil.zip","kind":"app","size":1,"sha256":"` + sha + `"}]}`,
		`{"version":"1.0.0","assets":[{"name":"a.zip","kind":"app","size":1,"sha256":"short"}]}`,
		`{"version":"1.0.0","assets":[{"name":"a.zip","kind":"app","size":0,"sha256":"` + sha + `"}]}`,
	} {
		if _, err := ParseManifest([]byte(bad)); err == nil {
			t.Fatalf("应拒绝 %s", bad)
		}
	}
}

func TestSemverOrdering(t *testing.T) {
	// SemVer 2.0.0 §11 示例顺序,外加本项目的预发布号
	ordered := []string{
		"1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta", "1.0.0-beta.2",
		"1.0.0-beta.11", "1.0.0-m9.1", "1.0.0-m9.2", "1.0.0-m9.10", "1.0.0-rc.1", "1.0.0", "1.0.1", "1.1.0", "2.0.0",
	}
	for i := 0; i+1 < len(ordered); i++ {
		if !Newer(ordered[i+1], ordered[i]) || Newer(ordered[i], ordered[i+1]) {
			t.Fatalf("%s 应新于 %s", ordered[i+1], ordered[i])
		}
	}
	if Newer("1.0.0", "1.0.0") || Newer("v1.0.0+build.5", "1.0.0") {
		t.Fatal("同版本(构建元数据不计)不算新")
	}
	for _, bad := range []string{"1.0", "1.0.0.0", "01.0.0", "1.0.0-", "1.0.0-a..b", "x.y.z"} {
		if _, err := ParseVersion(bad); err == nil {
			t.Fatalf("应拒绝版本号 %q", bad)
		}
		if Newer(bad, "0.0.1") || Newer("9.9.9", bad) {
			t.Fatalf("非法版本号 %q 不应被判为更新", bad)
		}
	}
}
