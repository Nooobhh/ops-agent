//go:build !windows

// 外壳只做 Windows;其他平台只为 go test/vet 能编译本包。
package main

import (
	"fmt"
	"os"
)

func main() {
	fmt.Fprintln(os.Stderr, "OpsAgent 外壳只支持 Windows;Linux 用 opsagent 命令(scripts/opsagent.py)")
	os.Exit(2)
}
