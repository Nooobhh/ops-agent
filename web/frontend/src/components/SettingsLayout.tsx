import type { ReactNode } from "react"
import { NavLink, useLocation } from "react-router-dom"
import { cn } from "@/lib/utils"


export type SettingsGroup = "alerts" | "resources" | "agent" | "plugins" | "knowledge" | "worklog" | "settings"

type SettingsNavItem = {
  to: string
  label: string
}

const PRIMARY_SETTINGS: Array<SettingsNavItem & { group: SettingsGroup }> = [
  { group: "alerts", label: "告警", to: "/settings/alerts" },
  { group: "resources", label: "资源", to: "/settings/resource-alerts" },
  { group: "agent", label: "Agent", to: "/settings/agent/permissions" },
  { group: "plugins", label: "插件", to: "/settings/plugins" },
  { group: "knowledge", label: "知识库", to: "/settings/knowledge/vector" },
  { group: "worklog", label: "工作日志", to: "/settings/weekly-report" },
  { group: "settings", label: "设置", to: "/settings/notify" },
]

const SECONDARY_SETTINGS: Record<SettingsGroup, SettingsNavItem[]> = {
  alerts: [
    { label: "告警规则", to: "/settings/alerts" },
    { label: "采集与存储", to: "/settings/alerts/acquisition" },
  ],
  resources: [
    { label: "资源告警", to: "/settings/resource-alerts" },
    { label: "采集与存储", to: "/settings/acquisition" },
  ],
  agent: [
    { label: "默认权限", to: "/settings/agent/permissions" },
    { label: "Agent 类型策略", to: "/settings/agent/policies" },
  ],
  plugins: [],
  knowledge: [
    { label: "向量配置", to: "/settings/knowledge/vector" },
    { label: "重排配置", to: "/settings/knowledge/rerank" },
  ],
  worklog: [],
  settings: [
    { label: "通知", to: "/settings/notify" },
    { label: "模型", to: "/settings/models" },
    { label: "账号", to: "/settings/credentials" },
    { label: "采集与存储", to: "/settings/maintenance" },
    { label: "本机用户", to: "/settings/user" },
    { label: "关于", to: "/settings/about" },
  ],
}

const GROUP_LABELS: Record<SettingsGroup, string> = {
  alerts: "告警",
  resources: "资源",
  agent: "Agent",
  plugins: "插件",
  knowledge: "知识库",
  worklog: "工作日志",
  settings: "设置",
}

export function settingsGroupForPath(pathname: string): SettingsGroup {
  if (pathname === "/settings/alerts" || pathname.startsWith("/settings/alerts/")) return "alerts"
  if (pathname === "/settings/acquisition" || pathname === "/settings/resource-alerts") return "resources"
  if (pathname.startsWith("/settings/agent")) return "agent"
  if (pathname === "/settings/plugins") return "plugins"
  if (pathname.startsWith("/settings/knowledge") || pathname === "/settings/external") return "knowledge"
  if (pathname === "/settings/weekly-report") return "worklog"
  return "settings"
}

function primaryLinkClass(active: boolean) {
  return cn(
    "border-b-2 border-transparent px-1 pb-3 text-sm font-medium text-muted-foreground transition-colors hover:text-foreground",
    active && "border-primary text-foreground",
  )
}

function secondaryLinkClass({isActive}: {isActive: boolean}) {
  return cn(
    "block rounded-md px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-muted/60 hover:text-foreground",
    isActive && "bg-accent font-medium text-accent-foreground",
  )
}

export function SettingsNav() {
  const location = useLocation()
  const activeGroup = settingsGroupForPath(location.pathname)
  return (
    <nav aria-label="设置模块" className="settings-primary-tabs">
      {PRIMARY_SETTINGS.map((item) => (
        <NavLink
          className={primaryLinkClass(activeGroup === item.group)}
          end={item.group === "alerts" || item.group === "resources" || item.group === "worklog" || item.group === "settings"}
          key={item.group}
          to={item.to}
        >
          {item.label}
        </NavLink>
      ))}
    </nav>
  )
}

export function SettingsSidebar({group}: {group: SettingsGroup}) {
  const items = SECONDARY_SETTINGS[group]
  if (!items.length) return null
  return (
    <aside aria-label={`${GROUP_LABELS[group]}设置`} className="settings-sidebar">
      <p className="settings-sidebar-title">{GROUP_LABELS[group]}设置</p>
      <nav>
        {items.map((item) => (
          <NavLink className={secondaryLinkClass} end key={item.to} to={item.to}>
            {item.label}
          </NavLink>
        ))}
      </nav>
    </aside>
  )
}

export function SettingsPageLayout({
  group,
  children,
}: {
  group: SettingsGroup
  children: ReactNode
}) {
  const hasSidebar = SECONDARY_SETTINGS[group].length > 0
  return (
    <div className="settings-page">
      <SettingsNav />
      <div className="settings-workspace" data-has-sidebar={hasSidebar}>
        <SettingsSidebar group={group} />
        <div className="settings-content">{children}</div>
      </div>
    </div>
  )
}
