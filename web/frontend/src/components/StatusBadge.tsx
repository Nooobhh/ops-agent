import { Badge } from "@/components/ui/badge"

type Props = { value?: string | null }

const labels: Record<string, string> = {
  active: "活跃", resolved: "已恢复", suppressed: "已抑制",
  critical: "严重", warning: "警告", info: "提示",
  running: "运行中", starting: "启动中", stalled: "已停摆", stopped: "已停止",
}

export function StatusBadge({ value }: Props) {
  const normalized = (value || "unknown").toLowerCase()
  return (
    <Badge className="status-badge" data-status={normalized} variant="outline">
      {labels[normalized] || value || "-"}
    </Badge>
  )
}
