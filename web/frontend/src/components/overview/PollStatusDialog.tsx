import { useState } from "react"
import { AlertTriangle, CheckCircle2, Power, XCircle } from "lucide-react"
import { ResourcePollStatus } from "@/components/resources/ResourcePollStatus"
import { StatusBadge } from "@/components/StatusBadge"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { formatTime } from "@/lib/api"
import { cn } from "@/lib/utils"
import type { PollStatus, ResourceSummaryResponse } from "@/types"

type View = "alerts" | "resources"

function healthTone(healthy: number, total: number, state?: string | null) {
  if (state === "starting") return "bg-amber-500"
  if (total > 0 && healthy === total) return "bg-emerald-500"
  if (healthy > 0) return "bg-amber-500"
  return "bg-red-500"
}

export function PollStatusDialog({ alertPoll, resourceSummary, busy, onToggleAlert }: {
  alertPoll: PollStatus | null
  resourceSummary: ResourceSummaryResponse | null
  busy: boolean
  onToggleAlert: () => void
}) {
  const [open, setOpen] = useState(false)
  const [view, setView] = useState<View>("alerts")
  const alertTotal = Object.keys(alertPoll?.sources || {}).length
  const alertHealthy = alertPoll?.runtime_state === "running"
    ? Object.values(alertPoll.sources).filter((source) => source.last_result === "success").length
    : 0
  const resourceTotal = Math.max((resourceSummary?.platforms.length || 0) + 1, 3)
  const resourceHealthy = resourceSummary?.runtime_state === "running"
    ? 1 + resourceSummary.platforms.filter((platform) => platform.status === "ok").length
    : 0
  function show(next: View) { setView(next); setOpen(true) }
  return <Dialog onOpenChange={setOpen} open={open}>
    <div className="inline-flex items-center gap-3" role="group" aria-label="轮询健康度">
      <button className="inline-flex items-center gap-2 text-xs font-normal text-[#9fb2ca] hover:text-white focus-visible:rounded-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-white/60" onClick={() => show("alerts")} type="button">告警 {alertHealthy}/{alertTotal}<span className={cn("size-2 rounded-full", healthTone(alertHealthy, alertTotal, alertPoll?.runtime_state))} /></button>
      <button className="inline-flex items-center gap-2 text-xs font-normal text-[#9fb2ca] hover:text-white focus-visible:rounded-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-white/60" onClick={() => show("resources")} type="button">资源 {resourceHealthy}/{resourceTotal}<span className={cn("size-2 rounded-full", healthTone(resourceHealthy, resourceTotal, resourceSummary?.runtime_state))} /></button>
    </div>
    <DialogContent className="max-w-2xl">
      <DialogHeader><DialogTitle>轮询状态</DialogTitle><DialogDescription>选择告警或资源轮询，查看任务心跳和来源明细。</DialogDescription></DialogHeader>
      <div className="flex gap-2"><Button onClick={() => setView("alerts")} size="sm" variant={view === "alerts" ? "default" : "outline"}>告警轮询</Button><Button onClick={() => setView("resources")} size="sm" variant={view === "resources" ? "default" : "outline"}>资源轮询</Button></div>
      {view === "alerts" ? <AlertPollDetail busy={busy} onToggle={onToggleAlert} poll={alertPoll} /> : <ResourcePollStatus summary={resourceSummary} />}
    </DialogContent>
  </Dialog>
}

function AlertPollDetail({ poll, busy, onToggle }: {
  poll: PollStatus | null
  busy: boolean
  onToggle: () => void
}) {
  const runtime = poll?.runtime_state || "stopped"
  return <section className="border-y py-4">
    <div className="flex flex-wrap items-center gap-3"><div className="mr-auto"><h3 className="text-sm font-semibold">告警轮询</h3><p className="text-xs text-muted-foreground">上次 tick：{formatTime(poll?.last_tick_at)}</p></div><StatusBadge value={runtime} /><Button disabled={!poll || busy} onClick={onToggle} size="sm" variant={poll?.effective ? "outline" : "destructive"}><Power />{poll?.effective ? "停止轮询" : "启用轮询"}</Button></div>
    <div className="mt-4 grid gap-3 sm:grid-cols-2">{Object.entries(poll?.sources || {}).map(([source, status]) => {
      const ok = runtime === "running" && status.last_result === "success"
      const starting = runtime === "starting"
      const Icon = ok ? CheckCircle2 : starting ? AlertTriangle : XCircle
      return <div className="flex items-start gap-2 border-l-2 pl-3" key={source}><Icon className={ok ? "mt-0.5 size-4 text-emerald-600" : starting ? "mt-0.5 size-4 text-amber-600" : "mt-0.5 size-4 text-red-600"} /><div className="min-w-0"><strong className="font-mono text-xs">{source}</strong><p className="text-xs text-muted-foreground">{ok ? "最近成功" : starting ? "轮询启动中" : status.last_result === "failure" ? `连续失败 ${status.consecutive_failures} 次` : "数据已过期"}</p>{status.last_error_at ? <p className="text-[11px] text-muted-foreground">{formatTime(status.last_error_at)}</p> : null}{status.last_result === "failure" && status.last_error ? <p className="mt-1 line-clamp-3 break-all text-[11px] text-destructive" title={status.last_error}>{status.last_error}</p> : null}</div></div>
    })}</div>
  </section>
}
