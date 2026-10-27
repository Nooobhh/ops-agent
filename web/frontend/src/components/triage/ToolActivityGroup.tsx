import { useState } from "react"
import { ChevronDown, LoaderCircle, TerminalSquare } from "lucide-react"
import type { ToolBlock } from "@/lib/triageEvents"
import { ToolActivity } from "./ToolActivity"


export function ToolActivityGroup({
  activities,
  taskId,
}: {
  activities: ToolBlock[]
  taskId: string
}) {
  const active = activities.some((item) => (
    item.status === "running" || item.status === "pending" || item.status === "needs_input"
  ))
  const [open, setOpen] = useState(active)
  const failed = activities.filter((item) => item.status === "failed" || item.status === "rejected").length
  const notExecuted = activities.filter((item) => (
    item.status === "skipped" || item.status === "rejected" || item.status === "needs_input"
  )).length
  const node = activities.find((item) => item.node)?.node || "未指定节点"
  const summaryCommand = activities.find((item) => item.toolName === "run_command")?.label
    || activities.find((item) => item.commands.length)?.commands[0]
    || activities[0]?.label
    || "只读检查"

  return (
    <details
      className="triage-tool-group"
      onToggle={(event) => setOpen(event.currentTarget.open)}
      open={open}
    >
      <summary className="flex cursor-pointer list-none items-center gap-3 p-3 hover:bg-muted/60">
        <span className="triage-tool-icon" aria-hidden="true">
          {active ? <LoaderCircle className="size-4 animate-spin" /> : <TerminalSquare className="size-4" />}
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs font-medium">
            <span>执行 {activities.length} 项只读检查</span>
            <span className="text-muted-foreground">· {node}</span>
            {failed ? <span className="text-destructive">· 失败 {failed}</span> : null}
            {notExecuted ? <span className="text-muted-foreground">· 未执行 {notExecuted}</span> : null}
          </span>
          <code className="mt-1 block truncate text-[11px] text-muted-foreground" title={summaryCommand}>
            {summaryCommand}
          </code>
        </span>
        <ChevronDown className={`size-4 shrink-0 text-muted-foreground transition-transform ${open ? "rotate-180" : ""}`} aria-hidden="true" />
      </summary>
      <div className="grid gap-2 border-t p-2">
        {activities.map((activity) => (
          <ToolActivity activity={activity} key={activity.eventId} taskId={taskId} />
        ))}
      </div>
    </details>
  )
}
