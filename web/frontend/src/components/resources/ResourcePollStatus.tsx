import { AlertTriangle, CheckCircle2, XCircle } from "lucide-react"
import { StatusBadge } from "@/components/StatusBadge"
import { formatTime } from "@/lib/api"
import type { ResourceSummaryResponse } from "@/types"

export function ResourcePollStatus({ summary }: { summary: ResourceSummaryResponse | null }) {
  const runtimeState = summary?.runtime_state || "stopped"
  const runtimeOk = runtimeState === "running"
  return <section className="border-y py-4">
    <div className="flex flex-wrap items-center gap-3">
      <div className="mr-auto"><h3 className="text-sm font-semibold">资源轮询</h3><p className="text-xs text-muted-foreground">上次 tick：{formatTime(summary?.last_success_at || summary?.last_collected_at)} · 周期 {summary?.interval_seconds || 300} 秒</p></div>
      <StatusBadge value={runtimeState} />
    </div>
    <div className="mt-4 grid gap-3 sm:grid-cols-3">
      <SourceStatus detail={runtimeOk ? "最近成功" : summary?.last_error || `连续失败 ${summary?.consecutive_failures || 0} 次`} name="prometheus-central" ok={runtimeOk} />
      {(summary?.platforms || []).map((platform) => <SourceStatus detail={`${runtimeOk ? "" : "快照已过期 · "}节点 ${platform.observed_nodes}/${platform.expected_nodes} · 服务 ${platform.observed_services}/${platform.expected_services}`} key={platform.platform} name={platform.platform.toUpperCase()} ok={runtimeOk && platform.status === "ok"} partial={runtimeOk && platform.status === "partial"} />)}
    </div>
  </section>
}

function SourceStatus({ name, detail, ok, partial = false }: {
  name: string
  detail: string
  ok: boolean
  partial?: boolean
}) {
  const Icon = ok ? CheckCircle2 : partial ? AlertTriangle : XCircle
  return <div className="flex items-start gap-2 border-l-2 pl-3"><Icon className={ok ? "mt-0.5 size-4 text-emerald-600" : partial ? "mt-0.5 size-4 text-orange-600" : "mt-0.5 size-4 text-red-600"} /><div><strong className="font-mono text-xs">{name}</strong><p className="text-xs text-muted-foreground">{detail}</p></div></div>
}
