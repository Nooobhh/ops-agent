import {
  Ban,
  CheckCircle2,
  ChevronRight,
  CircleAlert,
  CircleX,
  LoaderCircle,
  TerminalSquare,
} from "lucide-react"
import type { ToolBlock } from "@/lib/triageEvents"
import { ToolDetailDialog } from "./ToolDetailDialog"


const activityState = {
  running: {label: "执行中", icon: LoaderCircle},
  pending: {label: "Agent运行中", icon: LoaderCircle},
  needs_input: {label: "需要补充", icon: CircleAlert},
  approval_pending: {label: "等待审批", icon: CircleAlert},
  partial: {label: "部分完成", icon: CheckCircle2},
  done: {label: "完成", icon: CheckCircle2},
  failed: {label: "失败", icon: CircleX},
  rejected: {label: "未执行·已拒绝", icon: CircleX},
  skipped: {label: "未执行·已跳过", icon: Ban},
  interrupted: {label: "已中断", icon: Ban},
} as const


export function ToolActivity({
  activity,
  taskId,
}: {
  activity: ToolBlock
  taskId: string
}) {
  const state = activityState[activity.status]
  const StateIcon = state.icon
  const stateLabel = activity.status === "done" && activity.commandStatus === "nonzero"
    ? "完成·退出非0"
    : state.label

  return (
    <ToolDetailDialog activity={activity} taskId={taskId}>
      <button
        className="triage-tool w-full text-left transition-colors hover:bg-muted/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        type="button"
      >
        <span className="flex min-w-0 items-start gap-3">
          <span className="triage-tool-icon" aria-hidden="true">
            <TerminalSquare className="size-4" />
          </span>
          <span className="min-w-0 flex-1">
            <span className="flex flex-wrap items-center justify-between gap-2">
              <strong className="truncate font-mono text-xs" title={activity.label || activity.actionId}>
                {activity.label || "登记动作"}
              </strong>
              <span className={`triage-tool-state triage-tool-state-${activity.status}`}>
                <StateIcon
                  className={activity.status === "running" || activity.status === "pending" ? "size-3.5 animate-spin" : "size-3.5"}
                  aria-hidden="true"
                />
                {stateLabel}
              </span>
            </span>
            <span className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
              <span>{activity.scope || "未指定范围"}</span>
              {activity.commandStatus && activity.commandStatus !== "not_applicable"
                ? <span>退出 {activity.commandStatus}</span>
                : null}
              {activity.durationSeconds !== null
                ? <span>{activity.durationSeconds.toFixed(1)} 秒</span>
                : null}
            </span>
          </span>
          <ChevronRight className="mt-2 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
        </span>
      </button>
    </ToolDetailDialog>
  )
}
