import type { AlertDetailResponse, AlertTaskContext } from "../types.ts"


export type TriageNavigationDraft = {
  message: string
  platform?: string
  node?: string
  alert_context?: AlertTaskContext
  // 从工作项发起:建成即挂到该工作项
  work_item?: {id: string; title: string}
}

export type TriageNavigationState = {
  triageDraft: TriageNavigationDraft
}


export function alertTaskDraft(
  alert: Pick<AlertDetailResponse, "alertname" | "platform" | "host" | "source" | "key" | "description" | "current" | "payload">,
): TriageNavigationDraft {
  const draft: TriageNavigationDraft = {
    message: `排查告警：${alert.alertname}`,
  }
  const platform = knownPlatform(alert.platform)
  if (platform) draft.platform = platform
  if (alert.host) draft.node = alert.host
  // 告警源类型(prometheus 或环境包扩展的类型);告警键首段即类型
  const source = typeof alert.source === "string" && /^[a-z][a-z0-9_-]{0,31}$/.test(alert.source.split(":")[0])
    ? alert.source.split(":")[0]
    : null
  if (source) {
    const sourcePayload = alert.payload.data || {}
    const labels = recordValue(sourcePayload.labels)
    const annotations = recordValue(sourcePayload.annotations)
    draft.alert_context = {
      source,
      fingerprint: stringValue(sourcePayload.fingerprint),
      alertname: alert.alertname,
      labels,
      annotations: {
        ...annotations,
        ...(alert.description ? {description: alert.description} : {}),
      },
      platform: platform || null,
      host_raw: alert.host,
      node: alert.host,
      service: alert.current?.service || null,
      severity: alert.current?.severity || null,
      state: alert.current?.status || null,
      active_at: alert.current?.source_active_at || null,
      value: stringValue(sourcePayload.value),
      detail: alert.description,
      raw_ref: alert.key,
    }
  }
  return draft
}


// 「unknown」是告警归不到集群时的占位,不当作平台带给排查任务
function knownPlatform(value: unknown): string | null {
  return typeof value === "string" && value && value !== "unknown" ? value : null
}


function recordValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}


function stringValue(value: unknown): string | null {
  return typeof value === "string" ? value : null
}


export function taskHref(taskId: string): string {
  const params = new URLSearchParams({task: taskId})
  return `/triage?${params}`
}


export function draftFromNavigationState(
  state: unknown,
): TriageNavigationDraft | null {
  if (!state || typeof state !== "object") return null
  const value = (state as {triageDraft?: unknown}).triageDraft
  if (!value || typeof value !== "object") return null
  const raw = value as Record<string, unknown>
  if (typeof raw.message !== "string" || !raw.message.trim()) return null
  const draft: TriageNavigationDraft = {message: raw.message}
  const platform = knownPlatform(raw.platform)
  if (platform) draft.platform = platform
  if (typeof raw.node === "string" && raw.node) draft.node = raw.node
  if (isAlertTaskContext(raw.alert_context)) {
    draft.alert_context = raw.alert_context
  }
  const workItem = raw.work_item as Record<string, unknown> | undefined
  if (workItem && typeof workItem.id === "string" && typeof workItem.title === "string") {
    draft.work_item = {id: workItem.id, title: workItem.title}
  }
  return draft
}


function isAlertTaskContext(value: unknown): value is AlertTaskContext {
  if (!value || typeof value !== "object") return false
  const raw = value as Record<string, unknown>
  return (
    typeof raw.source === "string" && Boolean(raw.source)
    && typeof raw.alertname === "string"
    && Boolean(raw.alertname.trim())
  )
}
