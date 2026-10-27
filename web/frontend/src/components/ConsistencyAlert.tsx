import { AlertTriangle } from "lucide-react"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import type { Consistency } from "@/types"

export function ConsistencyAlert({ consistency }: { consistency?: Consistency }) {
  if (!consistency || consistency.ok) return null
  return (
    <Alert variant={consistency.level === "info" ? "default" : "destructive"} className="rounded-md">
      <AlertTriangle />
      <AlertTitle>{consistency.level === "info" ? "发现陈旧的未闭合记录" : "状态机与时间线近期不一致"}</AlertTitle>
      <AlertDescription>
        {consistency.detail || "数据源集合不一致"}{consistency.level === "info" ? "。该差异超过 24 小时，可在维护时处理。" : "。可能需要重建时间线。"}
      </AlertDescription>
    </Alert>
  )
}
