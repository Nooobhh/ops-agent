import { AlertTriangle, Cpu, HardDrive, MemoryStick } from "lucide-react"
import { Link } from "react-router-dom"
import { Card, CardContent, CardDescription, CardHeader } from "@/components/ui/card"
import { cn } from "@/lib/utils"
import type { ResourceSummaryResponse } from "@/types"

function coverageTone(observed: number, expected: number) {
  const ratio = expected > 0 ? observed / expected : 0
  if (ratio >= 0.95) return "text-emerald-600 dark:text-emerald-400"
  if (ratio >= 0.8) return "text-amber-600 dark:text-amber-400"
  return "text-red-600 dark:text-red-400"
}

function SummaryCard({ description, icon: Icon, value, note, valueTone = "text-foreground", href }: {
  description: string
  icon: typeof Cpu
  value: string | number
  note: string
  valueTone?: string
  href?: string
}) {
  const content = <Card className="h-full transition-colors hover:bg-muted/25">
    <CardHeader className="flex-row items-center justify-between">
      <CardDescription>{description}</CardDescription><Icon className="size-4 text-foreground" />
    </CardHeader>
    <CardContent><strong className={cn("text-2xl tabular-nums", valueTone)}>{value}</strong><p className="text-xs text-muted-foreground">{note}</p></CardContent>
  </Card>
  if (!href) return content
  const className = "block rounded-lg focus:outline-none focus:ring-2 focus:ring-ring"
  return href.startsWith("#")
    ? <a className={className} href={href}>{content}</a>
    : <Link className={className} to={href}>{content}</Link>
}

export function ResourceSummaryCards({ summary, links = false }: {
  summary: ResourceSummaryResponse | null
  links?: boolean
}) {
  const stats = summary?.stats
  const nodeTone = coverageTone(stats?.observed_nodes || 0, stats?.expected_nodes || 0)
  const serviceTone = coverageTone(stats?.observed_services || 0, stats?.expected_services || 0)
  return <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
    <SummaryCard description="节点覆盖" href={links ? "/resources?tab=nodes" : undefined} icon={Cpu} note="个 node_exporter 节点" value={`${stats?.observed_nodes || 0}/${stats?.expected_nodes || 36}`} valueTone={nodeTone} />
    <SummaryCard description="JVM/服务实例" href={links ? "/resources?tab=services" : undefined} icon={MemoryStick} note="可采 / 期望" value={`${stats?.observed_services || 0}/${stats?.expected_services || 0}`} valueTone={serviceTone} />
    <SummaryCard description="本地挂载点" href={links ? "/resources?tab=nodes" : undefined} icon={HardDrive} note="个 xfs/ext4 挂载点" value={stats?.local_mounts || 0} />
    <SummaryCard description="资源风险" href={links ? "/resources#risk-findings" : "#risk-findings"} icon={AlertTriangle} note={`条风险发现 · ${stats?.snapshot_count || 0} 条平台快照`} value={stats?.risk_count || 0} valueTone="text-red-600 dark:text-red-400" />
  </div>
}
