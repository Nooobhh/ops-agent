package apphost

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

// 中台进程控制,语义对齐 lib/apphost/controller.py:停止走请求文件正常退出,
// 超时才强杀整棵进程树;外部起的实例按 holder 文件接管,不重复拉起。

type State string

const (
	StateStopped      State = "stopped"
	StateStarting     State = "starting"
	StateRunning      State = "running"
	StateUnresponsive State = "unresponsive"
	StateStopping     State = "stopping"
	StateFailed       State = "failed"
)

var StateLabels = map[State]string{
	StateStopped:      "已停止",
	StateStarting:     "启动中",
	StateRunning:      "运行中",
	StateUnresponsive: "无响应",
	StateStopping:     "停止中",
	StateFailed:       "启动失败",
}

const (
	stopRequest    = "web_server.stop"
	holderFile     = "web_server.holder.json"
	serverLog      = "web_server.log"
	logRotateBytes = 5 << 20
)

type Server struct {
	DataDir     string
	StateDir    string
	Port        int
	StopTimeout time.Duration
	// PythonFor 按应用目录给出解释器;测试替换成假中台
	PythonFor func(appDir string) string
	client    *http.Client

	mu          sync.Mutex
	appDir      string
	proc        *os.Process
	done        chan struct{}
	exitCode    int
	everHealthy bool
	stopping    bool
	preparing   bool
	lastError   string
}

func NewServer(appDir, dataDir string, port int) *Server {
	return &Server{
		DataDir:     dataDir,
		StateDir:    filepath.Join(dataDir, "state"),
		Port:        port,
		StopTimeout: 45 * time.Second,
		PythonFor:   PythonPath,
		appDir:      appDir,
		// 不读系统代理:探活只打本机
		client: &http.Client{Timeout: 2 * time.Second, Transport: &http.Transport{Proxy: nil}},
	}
}

func (s *Server) AppDir() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.appDir
}

// SetAppDir 切换版本后指向新应用目录;只在中台停止时调用。
func (s *Server) SetAppDir(dir string) {
	s.mu.Lock()
	s.appDir = dir
	s.mu.Unlock()
}

func (s *Server) LogPath() string    { return filepath.Join(s.StateDir, serverLog) }
func (s *Server) ConsoleURL() string { return fmt.Sprintf("http://127.0.0.1:%d/", s.Port) }
func (s *Server) apiURL(path string) string {
	return fmt.Sprintf("http://127.0.0.1:%d%s", s.Port, path)
}

func (s *Server) LastError() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.lastError
}

func (s *Server) Healthy() bool {
	resp, err := s.client.Get(s.apiURL("/api/health"))
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	var body struct {
		Status string `json:"status"`
	}
	return resp.StatusCode == http.StatusOK && json.NewDecoder(resp.Body).Decode(&body) == nil && body.Status == "ok"
}

// WaitHealthy 轮询探活直到就绪、进程退出或超时。
func (s *Server) WaitHealthy(ctx context.Context, timeout time.Duration) bool {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		switch s.Refresh() {
		case StateRunning:
			return true
		case StateFailed, StateStopped:
			return false
		}
		select {
		case <-ctx.Done():
			return false
		case <-time.After(time.Second):
		}
	}
	return false
}

func (s *Server) Refresh() State {
	s.mu.Lock()
	stopping, preparing := s.stopping, s.preparing
	s.mu.Unlock()
	if stopping {
		return StateStopping
	}
	if preparing {
		return StateStarting
	}
	if s.Healthy() {
		s.mu.Lock()
		s.everHealthy, s.lastError = true, ""
		s.mu.Unlock()
		return StateRunning
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.proc != nil {
		select {
		case <-s.done:
			s.proc = nil
			if s.exitCode != 0 {
				if s.everHealthy {
					s.lastError = fmt.Sprintf("中台异常退出(code %d),详见日志", s.exitCode)
				} else {
					s.lastError = s.failureMessage(s.exitCode)
				}
			}
		default:
			if s.everHealthy {
				return StateUnresponsive
			}
			return StateStarting
		}
	}
	s.everHealthy = false
	if s.lastError != "" {
		return StateFailed
	}
	return StateStopped
}

// Start 已在运行(含外部起的实例)则不重复拉起。
func (s *Server) Start() error {
	if s.Healthy() {
		return nil
	}
	s.mu.Lock()
	if s.running() {
		s.mu.Unlock()
		return nil
	}
	s.lastError, s.everHealthy, s.preparing = "", false, true
	appDir := s.appDir
	s.mu.Unlock()

	if err := os.MkdirAll(s.StateDir, 0o755); err != nil {
		return s.fail("创建 state 目录失败: " + err.Error())
	}
	s.rotateLog()
	// 准备失败(如索引重建)不拦中台启动,知识页会显式报索引缺失/过期
	if warning := s.prepare(appDir); warning != "" {
		log.Print(warning)
	}
	s.mu.Lock()
	s.preparing = false
	s.mu.Unlock()

	logFile, err := os.OpenFile(s.LogPath(), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return s.fail("打开中台日志失败: " + err.Error())
	}
	defer logFile.Close()
	cmd := s.command(appDir, filepath.Join(appDir, "scripts", "web_server.py"),
		"--port", strconv.Itoa(s.Port), "--state-dir", s.StateDir)
	cmd.Stdout, cmd.Stderr = logFile, logFile
	if err := cmd.Start(); err != nil {
		return s.fail("启动中台失败: " + err.Error())
	}
	done := make(chan struct{})
	s.mu.Lock()
	s.proc, s.done = cmd.Process, done
	s.mu.Unlock()
	log.Printf("中台已拉起 pid=%d 目录 %s", cmd.Process.Pid, appDir)
	go func() {
		_ = cmd.Wait()
		s.mu.Lock()
		s.exitCode = cmd.ProcessState.ExitCode()
		s.mu.Unlock()
		close(done)
	}()
	return nil
}

func (s *Server) Stop() {
	pid, owned := s.pid()
	if pid == 0 {
		return
	}
	s.mu.Lock()
	s.stopping = true
	done := s.done
	s.mu.Unlock()
	stopFile := filepath.Join(s.StateDir, stopRequest)
	defer func() {
		os.Remove(stopFile)
		s.mu.Lock()
		s.stopping, s.everHealthy = false, false
		s.mu.Unlock()
	}()
	if err := os.WriteFile(stopFile, nil, 0o644); err != nil {
		log.Printf("写停止请求失败: %v", err)
	}
	deadline := time.Now().Add(s.StopTimeout)
	for time.Now().Before(deadline) {
		if owned {
			select {
			case <-done:
				return
			case <-time.After(500 * time.Millisecond):
			}
		} else {
			if !s.Healthy() {
				return
			}
			time.Sleep(500 * time.Millisecond)
		}
	}
	log.Printf("中台 %s 内未退出,强杀进程树 pid=%d", s.StopTimeout, pid)
	killTree(pid)
	if owned {
		select {
		case <-done:
		case <-time.After(10 * time.Second):
		}
	}
}

// pid 自己拉起的进程优先;否则只在探活成功时信 holder(中台退出后 holder 会残留)。
func (s *Server) pid() (int, bool) {
	s.mu.Lock()
	if s.running() {
		pid := s.proc.Pid
		s.mu.Unlock()
		return pid, true
	}
	s.mu.Unlock()
	data, err := os.ReadFile(filepath.Join(s.StateDir, holderFile))
	if err != nil {
		return 0, false
	}
	var holder struct {
		PID int `json:"pid"`
	}
	if json.Unmarshal(data, &holder) != nil || holder.PID == 0 || !s.Healthy() {
		return 0, false
	}
	return holder.PID, false
}

// running 调用方须持有 s.mu。
func (s *Server) running() bool {
	if s.proc == nil {
		return false
	}
	select {
	case <-s.done:
		return false
	default:
		return true
	}
}

func (s *Server) fail(message string) error {
	s.mu.Lock()
	s.lastError, s.preparing = message, false
	s.mu.Unlock()
	log.Print(message)
	return fmt.Errorf("%s", message)
}

func (s *Server) command(appDir string, args ...string) *exec.Cmd {
	cmd := exec.Command(s.PythonFor(appDir), args...)
	cmd.Dir = appDir
	// 重定向到文件时 Windows 默认按 GBK 写;中台按 UTF-8 开发
	cmd.Env = append(os.Environ(), "OPS_AGENT_DATA_ROOT="+s.DataDir, "PYTHONUTF8=1", "PYTHONIOENCODING=utf-8")
	hideConsole(cmd)
	return cmd
}

// prepare 经 opsagent.py prepare:首启与版本变化时重建知识索引(外壳与 CLI 共用一份 Python 逻辑)。
func (s *Server) prepare(appDir string) string {
	if _, err := os.Stat(filepath.Join(appDir, PackageInfoName)); err != nil {
		return ""
	}
	out, err := s.command(appDir, filepath.Join(appDir, "scripts", "opsagent.py"), "prepare").Output()
	if err == nil {
		return ""
	}
	var payload struct {
		Message string `json:"message"`
	}
	if json.Unmarshal(bytes.TrimSpace(out), &payload) == nil && payload.Message != "" {
		return "知识库准备失败: " + payload.Message
	}
	return "知识库准备失败: " + strings.TrimSpace(string(out)) + " " + err.Error()
}

func (s *Server) rotateLog() {
	if info, err := os.Stat(s.LogPath()); err == nil && info.Size() > logRotateBytes {
		_ = os.Rename(s.LogPath(), s.LogPath()+".1")
	}
}

// failureMessage 中台启动失败经 emit_err 打一行 JSON,取日志尾部最后一条的 message。
func (s *Server) failureMessage(code int) string {
	f, err := os.Open(s.LogPath())
	if err == nil {
		defer f.Close()
		if info, err := f.Stat(); err == nil && info.Size() > 8192 {
			_, _ = f.Seek(-8192, io.SeekEnd)
		}
		tail, _ := io.ReadAll(f)
		lines := bytes.Split(tail, []byte("\n"))
		for i := len(lines) - 1; i >= 0; i-- {
			var payload struct {
				Message string `json:"message"`
			}
			if json.Unmarshal(bytes.TrimSpace(lines[i]), &payload) == nil && payload.Message != "" {
				return payload.Message
			}
		}
	}
	return fmt.Sprintf("中台进程退出(code %d),详见日志", code)
}

// Activity 更新前确认用:进行中的排查任务与周报生成数。读现有只读接口,中台未运行时返回 0。
type Activity struct {
	Tasks      int `json:"tasks"`
	WeeklyJobs int `json:"weekly_jobs"`
}

func (a Activity) Busy() bool { return a.Tasks+a.WeeklyJobs > 0 }

func (s *Server) Activity() (Activity, error) {
	var out Activity
	if !s.Healthy() {
		return out, nil
	}
	var health struct {
		Queue map[string]int `json:"queue"`
	}
	if err := s.getJSON("/api/triage-health", &health); err != nil {
		return out, err
	}
	for _, status := range []string{"queued", "running", "waiting_operation"} {
		out.Tasks += health.Queue[status]
	}
	var jobs struct {
		Items []struct {
			Status string `json:"status"`
		} `json:"items"`
	}
	if err := s.getJSON("/api/weekly-report-jobs", &jobs); err != nil {
		return out, err
	}
	for _, job := range jobs.Items {
		if job.Status == "queued" || job.Status == "running" {
			out.WeeklyJobs++
		}
	}
	return out, nil
}

func (s *Server) getJSON(path string, v any) error {
	resp, err := s.client.Get(s.apiURL(path))
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("%s 返回 %d", path, resp.StatusCode)
	}
	return json.NewDecoder(resp.Body).Decode(v)
}
