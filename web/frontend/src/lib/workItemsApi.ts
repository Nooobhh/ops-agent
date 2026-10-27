import type {
  ImportResult,
  MatchSuggestion,
  WeeklyBucketsResponse,
  WorkItem,
  WorkItemDetailResponse,
  WorkItemLinkKind,
  WorkItemPatch,
  WorkItemPlan,
  WorkItemPlanDetail,
  WorkItemPlanSummary,
  WorkItemStatus,
  WorkItemSummary,
  WorklogWorkItemRef,
} from "./workItemsTypes.ts"

/** 409 带 owner_id(引用已挂到别的工作项 / 同一告警已有未关闭项)时可直接跳过去。 */
export class WorkItemApiError extends Error {
  ownerId: string | null

  constructor(message: string, ownerId: string | null = null) {
    super(message)
    this.ownerId = ownerId
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init.body ? { "Content-Type": "application/json" } : {}),
    },
  })
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: unknown } | null
    const detail = payload?.detail
    if (detail && typeof detail === "object" && "message" in detail) {
      const value = detail as { message?: string; owner_id?: string }
      throw new WorkItemApiError(value.message || `请求失败 (${response.status})`, value.owner_id || null)
    }
    throw new WorkItemApiError(typeof detail === "string" ? detail : `请求失败 (${response.status})`)
  }
  return response.json() as Promise<T>
}

function json(method: string, body: unknown): RequestInit {
  return { method, body: JSON.stringify(body) }
}

export type WorkItemFilters = {
  status?: WorkItemStatus[]
  includeClosed?: boolean
  planId?: string
  q?: string
}

export function listWorkItems(filters: WorkItemFilters = {}, signal?: AbortSignal): Promise<{ today: string; items: WorkItemSummary[] }> {
  const params = new URLSearchParams()
  for (const status of filters.status || []) params.append("status", status)
  if (filters.includeClosed) params.set("include_closed", "true")
  if (filters.planId) params.set("plan_id", filters.planId)
  if (filters.q?.trim()) params.set("q", filters.q.trim())
  const query = params.toString()
  return request(`/api/work-items${query ? `?${query}` : ""}`, { signal })
}

export function createWorkItem(fields: Partial<WorkItem> & { title: string }): Promise<{ item: WorkItem }> {
  return request("/api/work-items", json("POST", fields))
}

export function loadWorkItem(itemId: string, signal?: AbortSignal): Promise<WorkItemDetailResponse> {
  return request(`/api/work-items/${encodeURIComponent(itemId)}`, { signal })
}

export function updateWorkItem(itemId: string, patch: WorkItemPatch): Promise<{ item: WorkItem }> {
  return request(`/api/work-items/${encodeURIComponent(itemId)}`, json("PATCH", patch))
}

export function linkWorkItem(itemId: string, kind: WorkItemLinkKind, ref: string): Promise<{ item: WorkItem }> {
  return request(`/api/work-items/${encodeURIComponent(itemId)}/links`, json("POST", { kind, ref }))
}

export function unlinkWorkItem(itemId: string, kind: WorkItemLinkKind, ref: string): Promise<{ item: WorkItem }> {
  const params = new URLSearchParams({ kind, ref })
  return request(`/api/work-items/${encodeURIComponent(itemId)}/links?${params}`, { method: "DELETE" })
}

export function workItemByLink(
  kind: WorkItemLinkKind, ref: string, signal?: AbortSignal,
): Promise<{ item: WorkItemSummary | null; derived: boolean }> {
  const params = new URLSearchParams({ kind, ref })
  return request(`/api/work-items/by-link?${params}`, { signal })
}

export function loadWorklogWorkItems(
  startDate: string, endDate: string, signal?: AbortSignal,
): Promise<{ refs: Record<string, WorklogWorkItemRef> }> {
  const params = new URLSearchParams({ start_date: startDate, end_date: endDate })
  return request(`/api/work-items/worklog-map?${params}`, { signal })
}

export function loadWeeklyBuckets(startDate: string, endDate: string, signal?: AbortSignal): Promise<WeeklyBucketsResponse> {
  const params = new URLSearchParams({ start_date: startDate, end_date: endDate })
  return request(`/api/work-items/weekly?${params}`, { signal })
}

export function suggestMatches(startDate: string, endDate: string): Promise<{ suggestions: MatchSuggestion[]; unassigned: number }> {
  return request("/api/work-items/match-suggestions", json("POST", { start_date: startDate, end_date: endDate }))
}

export function listPlans(signal?: AbortSignal): Promise<{ plans: WorkItemPlanSummary[] }> {
  return request("/api/work-item-plans", { signal })
}

export function loadPlan(planId: string, signal?: AbortSignal): Promise<WorkItemPlanDetail> {
  return request(`/api/work-item-plans/${encodeURIComponent(planId)}`, { signal })
}

export function updatePlan(planId: string, patch: { title?: string; description?: string }): Promise<{ plan: WorkItemPlan }> {
  return request(`/api/work-item-plans/${encodeURIComponent(planId)}`, json("PATCH", patch))
}

export type ImportRequest = { text: string; plan_id?: string; plan_title?: string }

export function previewImport(body: ImportRequest): Promise<ImportResult> {
  return request("/api/work-item-imports/preview", json("POST", body))
}

export function applyImport(body: ImportRequest): Promise<ImportResult> {
  return request("/api/work-item-imports", json("POST", body))
}
