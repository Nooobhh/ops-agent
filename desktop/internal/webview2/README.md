# vendored go-webview2(MIT)

来源:`github.com/jchv/go-webview2@v0.0.0-20260205173254-56598839c808` 的 `pkg/edge` 与 `internal/w32`,许可证见同目录 `LICENSE`。
上游顶层包建窗即显示、`Destroy` 会退出消息循环,做不了面板懒创建与销毁,外壳只用底层 `edge` 并自建窗口。

本地改动(升级上游时逐条带上):
- `Chromium.Close()`:经控制器 vtbl 调 Close 并释放 webview/controller/environment 引用,WebView2 进程随之退出
- 环境/控制器创建失败不再 `log.Fatalf` 杀进程,改为 `Embed` 返回 false
- `MessageReceived` 不再把收到的消息原样回发
- 所有文件补 `//go:build windows`(删 `comproc_go17.go`,只用 Go ≥1.18),mac 上 `go test ./...` 能跳过本包
