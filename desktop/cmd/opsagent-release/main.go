// opsagent-release:发布签名工具,与外壳共用 internal/release 的签名/验签代码。
//
//	keygen [-key <私钥路径>]            生成密钥对,私钥写文件(0600,已存在则拒绝),公钥行打到 stdout
//	sign   [-key <私钥路径>] <文件>      写 <文件>.sig
//	verify -pub <公钥文件> <文件> [<签名>]
package main

import (
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"

	"github.com/Nooobhh/ops-agent/desktop/internal/release"
)

func defaultKeyPath() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".ops-agent", "release-signing.key")
}

func main() {
	if len(os.Args) < 2 {
		fail(errors.New("用法: opsagent-release keygen|sign|verify ..."))
	}
	fs := flag.NewFlagSet(os.Args[1], flag.ExitOnError)
	key := fs.String("key", defaultKeyPath(), "私钥路径")
	pub := fs.String("pub", "", "公钥文件(verify)")
	_ = fs.Parse(os.Args[2:])
	var err error
	switch os.Args[1] {
	case "keygen":
		err = keygen(*key)
	case "sign":
		err = sign(*key, fs.Args())
	case "verify":
		err = verify(*pub, fs.Args())
	default:
		err = fmt.Errorf("未知子命令 %q", os.Args[1])
	}
	if err != nil {
		fail(err)
	}
}

func keygen(path string) error {
	if _, err := os.Stat(path); err == nil {
		return fmt.Errorf("%s 已存在,拒绝覆盖(丢失私钥 = 所有客户端只能手动重装)", path)
	}
	pubLine, privText, err := release.GenerateKey()
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	if err := os.WriteFile(path, []byte(privText), 0o600); err != nil {
		return err
	}
	fmt.Println(pubLine)
	return nil
}

func sign(keyPath string, args []string) error {
	if len(args) != 1 {
		return errors.New("用法: sign [-key 私钥] <文件>")
	}
	text, err := os.ReadFile(keyPath)
	if err != nil {
		return fmt.Errorf("读私钥失败(发版只在持有私钥的 mac 上进行): %w", err)
	}
	priv, err := release.ParsePrivateKey(string(text))
	if err != nil {
		return err
	}
	data, err := os.ReadFile(args[0])
	if err != nil {
		return err
	}
	return os.WriteFile(args[0]+".sig", []byte(release.Sign(priv, data)), 0o644)
}

func verify(pubPath string, args []string) error {
	if pubPath == "" || len(args) < 1 || len(args) > 2 {
		return errors.New("用法: verify -pub <公钥文件> <文件> [<签名>]")
	}
	sigPath := args[0] + ".sig"
	if len(args) == 2 {
		sigPath = args[1]
	}
	pubText, err := os.ReadFile(pubPath)
	if err != nil {
		return err
	}
	keys, err := release.ParsePublicKeys(string(pubText))
	if err != nil {
		return err
	}
	data, err := os.ReadFile(args[0])
	if err != nil {
		return err
	}
	sig, err := os.ReadFile(sigPath)
	if err != nil {
		return err
	}
	if err := release.Verify(keys, data, sig); err != nil {
		return err
	}
	fmt.Println("ok")
	return nil
}

func fail(err error) {
	fmt.Fprintln(os.Stderr, "opsagent-release:", err)
	os.Exit(1)
}
