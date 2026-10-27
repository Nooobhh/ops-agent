import { useCallback, useEffect, useState } from "react"
import { Activity, AlertTriangle, BellRing, BookOpen, ClipboardList, Gauge, ListFilter, MessageSquareText, Moon, NotebookPen, Settings, Sun } from "lucide-react"
import { NavLink, Outlet, useLocation } from "react-router-dom"
import { BackToTop } from "@/components/BackToTop"
import { Button } from "@/components/ui/button"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { PollStatusDialog } from "@/components/overview/PollStatusDialog"
import { fetchJson, postJson } from "@/lib/api"
import { loadResourceSummary } from "@/lib/acquisitionApi"
import { setEnvTimeZone } from "@/lib/envTime"
import { setTriageTargets } from "@/lib/triageView"
import { loadLocalUser } from "@/lib/localUserApi"
import { enabledPluginHealth, loadPluginHealth } from "@/lib/pluginsApi"
import { isSettingsPath } from "@/lib/modelSettingsView"
import type { AppShellOutletContext, EnvInfo, HealthResponse, PluginHealthResponse, PollStatus, ResourceSummaryResponse } from "@/types"

export function AppShell() {
  const location = useLocation()
  const [health, setHealth] = useState<HealthResponse | null>(null)
  // 页面要按环境时区展示时间,拿到 /api/env(或确认取不到)之前不渲染页面
  const [envReady, setEnvReady] = useState(false)
  const [envOrigin, setEnvOrigin] = useState<EnvInfo["origin"] | null>(null)
  const [poll, setPoll] = useState<PollStatus | null>(null)
  const [resourceSummary, setResourceSummary] = useState<ResourceSummaryResponse | null>(null)
  const [pluginHealth, setPluginHealth] = useState<PluginHealthResponse | null>(null)
  const [pollBusy, setPollBusy] = useState(false)
  // null = 未知(接口不可用时不提示);false = 明确未设置本机用户
  const [localUserConfigured, setLocalUserConfigured] = useState<boolean | null>(null)
  const [dark, setDark] = useState(() => document.documentElement.dataset.theme === "dark")

  const loadHealth = useCallback(async () => {
    try { setHealth(await fetchJson<HealthResponse>("/api/health")) } catch { setHealth(null) }
  }, [])

  const loadPoll = useCallback(async () => {
    const [alertResult, resourceResult, pluginResult] = await Promise.allSettled([
      fetchJson<PollStatus>("/api/poll/status"),
      loadResourceSummary(),
      loadPluginHealth(),
    ])
    setPoll(alertResult.status === "fulfilled" ? alertResult.value : null)
    setResourceSummary(resourceResult.status === "fulfilled" ? resourceResult.value : null)
    setPluginHealth(pluginResult.status === "fulfilled" ? pluginResult.value : null)
  }, [])

  useEffect(() => {
    const refresh = () => {
      loadLocalUser()
        .then((response) => setLocalUserConfigured(response.configured))
        .catch(() => setLocalUserConfigured(null))
    }
    refresh()
    window.addEventListener("local-user-changed", refresh)
    return () => window.removeEventListener("local-user-changed", refresh)
  }, [])

  useEffect(() => {
    fetchJson<EnvInfo>("/api/env")
      .then((env) => { setEnvTimeZone(env.timezone); setTriageTargets(env.clusters); setEnvOrigin(env.origin) })
      .catch(() => undefined)
      .finally(() => setEnvReady(true))
  }, [])

  useEffect(() => {
    void loadHealth()
    void loadPoll()
    const timer = window.setInterval(() => { void loadHealth(); void loadPoll() }, 60_000)
    return () => window.clearInterval(timer)
  }, [loadHealth, loadPoll])

  function toggleTheme() {
    const next = !dark
    setDark(next)
    document.documentElement.dataset.theme = next ? "dark" : "light"
    try { localStorage.setItem("ops-theme", next ? "dark" : "light") } catch (_) { /* no-op */ }
  }

  async function toggleAlertPoll() {
    if (!poll || pollBusy) return
    setPollBusy(true)
    try {
      setPoll(await postJson<PollStatus>("/api/poll/toggle", { enabled: !poll.effective }))
    } catch {
      await loadPoll()
    } finally {
      setPollBusy(false)
    }
  }

  const available = health ? Object.values(health.data_sources).filter(Boolean).length : 0
  const plugins = enabledPluginHealth(pluginHealth)
  const sourceTotal = health ? Object.keys(health.data_sources).length : 0
  return (
    <div className="min-h-screen bg-background">
      <header className="sticky top-0 z-20 border-b border-white/10 bg-[var(--top-bg)] text-[var(--top-fg)]">
        <div className="mx-auto flex h-14 max-w-[1480px] items-center gap-4 px-3 sm:px-4 lg:gap-5 lg:px-6">
          <NavLink className="flex min-w-0 items-center gap-2 font-semibold" to="/">
            <BellRing className="size-5 text-[#7fa8e8]" aria-hidden="true" />
            <span className="truncate">OpsAgent 运维中台</span>
          </NavLink>
          <nav className="flex h-full items-center gap-1" aria-label="主导航">
            <NavLink aria-label="概览" className={({ isActive }) => `top-nav ${isActive ? "top-nav-active" : ""}`} end title="概览" to="/">
              <Activity className="size-4" aria-hidden="true" />概览
            </NavLink>
            <NavLink aria-label="告警" className={({ isActive }) => `top-nav ${isActive ? "top-nav-active" : ""}`} title="告警" to="/alerts">
              <ListFilter className="size-4" aria-hidden="true" />告警
            </NavLink>
            <NavLink aria-label="资源" className={({ isActive }) => `top-nav ${isActive ? "top-nav-active" : ""}`} title="资源状态" to="/resources">
              <Gauge className="size-4" aria-hidden="true" />资源
            </NavLink>
            <NavLink aria-label="Agent-Ops" className={({ isActive }) => `top-nav ${isActive ? "top-nav-active" : ""}`} title="Agent-Ops" to="/triage">
              <MessageSquareText className="size-4" aria-hidden="true" />Agent-Ops
            </NavLink>
            <NavLink aria-label="知识库" className={({ isActive }) => `top-nav ${isActive ? "top-nav-active" : ""}`} title="知识库" to="/knowledge">
              <BookOpen className="size-4" aria-hidden="true" />知识库
            </NavLink>
            <NavLink aria-label="工作项" className={({ isActive }) => `top-nav ${isActive ? "top-nav-active" : ""}`} title="工作项与计划" to="/work-items">
              <ClipboardList className="size-4" aria-hidden="true" />工作项
            </NavLink>
            <NavLink aria-label="工作日志" className={({ isActive }) => `top-nav ${isActive ? "top-nav-active" : ""}`} title="工作日志与周报" to="/worklog">
              <NotebookPen className="size-4" aria-hidden="true" />工作日志
            </NavLink>
            <NavLink aria-label="设置" className={({ isActive }) => `top-nav ${isActive || isSettingsPath(location.pathname) ? "top-nav-active" : ""}`} title="设置" to="/settings/notify">
              <Settings className="size-4" aria-hidden="true" />设置
            </NavLink>
          </nav>
          <div className="ml-auto flex shrink-0 items-center gap-2 text-xs text-[#9fb2ca] sm:gap-3">
            <span className="hidden sm:inline">数据源 {available}/{sourceTotal}</span>
            <span className={`size-2 rounded-full ${sourceTotal > 0 && available === sourceTotal ? "bg-[#2f9e6e]" : "bg-[#d69e2e]"}`} />
            {plugins.total > 0 ? <NavLink className="flex items-center gap-2 hover:text-[var(--top-fg)]" title="插件健康(已启用)" to="/settings/plugins">
              <span className="hidden xl:inline">插件 {plugins.healthy}/{plugins.total}</span>
              <span className={`size-2 rounded-full ${plugins.healthy === plugins.total ? "bg-[#2f9e6e]" : "bg-[#d64545]"}`} />
            </NavLink> : null}
            <PollStatusDialog alertPoll={poll} busy={pollBusy} onToggleAlert={() => void toggleAlertPoll()} resourceSummary={resourceSummary} />
            <Button aria-label="切换主题" className="text-[var(--top-fg)] hover:bg-white/15" onClick={toggleTheme} size="icon" title="切换浅色 / 深色主题" variant="ghost">
              {dark ? <Sun /> : <Moon />}
            </Button>
          </div>
        </div>
      </header>
      {poll && !poll.effective ? (
        <div className="border-b border-destructive/40 bg-destructive/15">
          <div className="mx-auto flex max-w-[1480px] items-center gap-3 px-4 py-3 lg:px-6">
            <AlertTriangle className="size-5 shrink-0 text-destructive" />
            <div className="min-w-0 flex-1"><strong className="text-sm text-destructive">告警轮询已停止，当前不会收到任何告警</strong><p className="text-xs text-muted-foreground">{poll.reason}</p></div>
          </div>
        </div>
      ) : null}
      {envOrigin === "example" ? (
        <div className="border-b border-[#d69e2e]/40 bg-[#d69e2e]/10">
          <div className="mx-auto flex max-w-[1480px] items-center gap-3 px-4 py-2 text-sm text-foreground lg:px-6">
            <span className="min-w-0 flex-1">未挂载环境数据，当前为示例环境（集群、地址与插件均为虚构）。</span>
            {location.pathname !== "/settings/about" ? <NavLink className="shrink-0 font-medium text-primary hover:underline" to="/settings/about">查看说明</NavLink> : null}
          </div>
        </div>
      ) : null}
      {localUserConfigured === false && location.pathname !== "/settings/user" ? (
        <div className="border-b border-[#d69e2e]/40 bg-[#d69e2e]/10">
          <div className="mx-auto flex max-w-[1480px] items-center gap-3 px-4 py-2 text-sm text-foreground lg:px-6">
            <span className="min-w-0 flex-1">未设置本机用户：新写入的工作日志与排查记录暂不带作者。</span>
            <NavLink className="shrink-0 font-medium text-primary hover:underline" to="/settings/user">前往设置</NavLink>
          </div>
        </div>
      ) : null}
      {health?.timeline.status === "recovering" ? (
        <div className="border-b border-[#d69e2e]/40 bg-[#d69e2e]/10"><div className="mx-auto max-w-[1480px] px-4 py-2 text-sm text-foreground lg:px-6">{health.timeline.detail}</div></div>
      ) : null}
      {health && !health.consistency.ok && location.pathname !== "/" ? (
        <div className={health.consistency.level === "info"
          ? "border-b border-border bg-muted/35"
          : "border-b border-destructive/30 bg-destructive/10"}>
          <div className="mx-auto max-w-[1480px] px-4 py-2 lg:px-6">
            <Alert className={`border-0 bg-transparent p-0 ${health.consistency.level === "info" ? "text-foreground" : "text-destructive"}`}>
              <AlertTriangle />
              <AlertDescription>{health.consistency.detail}{health.consistency.level === "info" ? "。该差异超过 24 小时，可在维护时处理。" : "。可能需要重建时间线。"}</AlertDescription>
            </Alert>
          </div>
        </div>
      ) : null}
      <main className="mx-auto max-w-[1480px] px-4 py-6 lg:px-6 lg:py-8">
        {envReady ? (
          <Outlet context={{
            poll,
            resourceSummary,
            pollBusy,
            toggleAlertPoll,
          } satisfies AppShellOutletContext} />
        ) : null}
      </main>
      <BackToTop />
    </div>
  )
}
