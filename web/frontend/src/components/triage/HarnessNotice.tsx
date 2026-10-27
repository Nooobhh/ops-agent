import { AlarmClock, ClipboardList, Flag, Gauge, Info, OctagonAlert } from "lucide-react"
import type { NoticeVariant } from "@/lib/triageEvents"


const VARIANTS: Record<NoticeVariant, {title: string; icon: typeof Info; tone: string}> = {
  opening: {title: "任务开场材料(Harness 预先准备)", icon: ClipboardList, tone: ""},
  reminder: {title: "预算提醒", icon: AlarmClock, tone: "border-[#d69e2e] bg-[#d69e2e]/10"},
  closing: {title: "收口回合", icon: Flag, tone: "border-[#d69e2e] bg-[#d69e2e]/15"},
  unanswered: {title: "未收口", icon: OctagonAlert, tone: "border-[#c53030] bg-[#c53030]/10"},
  tier: {title: "档位变更", icon: Gauge, tone: ""},
  notice: {title: "Harness 说明", icon: Info, tone: ""},
}


export function HarnessNotice({variant, text}: {variant: NoticeVariant; text: string}) {
  const value = VARIANTS[variant]
  const Icon = value.icon
  if (variant === "opening" || variant === "notice") {
    // 开场材料可能很长(快照、引用清单、playbook 结果),默认收起
    return (
      <details className="rounded-md border bg-muted/30 px-3 py-2 text-xs">
        <summary className="flex cursor-pointer list-none items-center gap-2 text-muted-foreground">
          <Icon className="size-3.5 shrink-0" aria-hidden="true" />
          {value.title}
        </summary>
        <pre className="mt-2 max-h-96 overflow-y-auto whitespace-pre-wrap break-all font-mono text-[11px]">{text}</pre>
      </details>
    )
  }
  if (variant === "tier") {
    return (
      <p className="flex items-center gap-2 text-xs text-muted-foreground">
        <Icon className="size-3.5 shrink-0" aria-hidden="true" />
        {text}
      </p>
    )
  }
  return (
    <div className={`flex items-start gap-2 border-l-2 px-3 py-2 text-xs ${value.tone}`}>
      <Icon className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
      <div className="min-w-0">
        <strong className="mr-1">{value.title}</strong>
        <span className="whitespace-pre-wrap break-words">{text.replace(/^\[[^\]]+\]\s*/, "")}</span>
      </div>
    </div>
  )
}
