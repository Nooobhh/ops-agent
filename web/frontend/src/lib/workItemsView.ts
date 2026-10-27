import type {
  WeeklyBucket,
  WeeklyBucketEntry,
  WorkItemHistoryEvent,
  WorkItemPriority,
  WorkItemSource,
  WorkItemStatus,
  WorkItemSummary,
} from "./workItemsTypes.ts"

export const STATUS_ORDER: WorkItemStatus[] = ["todo", "in_progress", "waiting", "done", "cancelled"]
export const STATUS_LABELS: Record<WorkItemStatus, string> = {
  todo: "待办",
  in_progress: "进行中",
  waiting: "等待",
  done: "完成",
  cancelled: "取消",
}
export const PRIORITY_LABELS: Record<WorkItemPriority, string> = { high: "高", normal: "中", low: "低" }
export const SOURCE_LABELS: Record<WorkItemSource, string> = {
  manual: "人工",
  alert: "告警",
  channel: "渠道",
  plan: "计划",
}
export const BUCKET_LABELS: Record<WeeklyBucket, string> = {
  completed: "本周完成",
  in_progress: "进行中",
  planned: "下周计划",
  open: "其他未关闭",
}
const FIELD_LABELS: Record<string, string> = {
  title: "标题",
  description: "描述",
  status: "状态",
  assignee: "负责人",
  priority: "优先级",
  start_date: "开始",
  due_date: "截止",
  plan_id: "计划",
  plan_node: "节点",
  nodes: "节点",
  link: "挂载",
}
const ACTION_LABELS: Record<WorkItemHistoryEvent["action"], string> = {
  created: "创建",
  imported: "导入",
  updated: "修改",
  status: "改状态",
  linked: "挂载",
  unlinked: "解除挂载",
}

export function isClosed(status: WorkItemStatus): boolean {
  return status === "done" || status === "cancelled"
}

export function isOverdue(item: Pick<WorkItemSummary, "status" | "due_date">, today: string): boolean {
  return !isClosed(item.status) && Boolean(item.due_date) && String(item.due_date) < today
}

export function statusBadgeClass(status: WorkItemStatus): string {
  switch (status) {
    case "in_progress": return "border-[#2f6fd6]/40 bg-[#eef4ff] text-[#1f4f9e] dark:bg-[#2f6fd6]/15 dark:text-[#9bbcf2]"
    case "waiting": return "border-[#b7791f]/40 bg-[#fff8eb] text-[#8a5a12] dark:bg-[#b7791f]/10 dark:text-[#e7b55d]"
    case "done": return "border-[#247a56]/40 bg-[#ecf8f2] text-[#1d6446] dark:bg-[#247a56]/15 dark:text-[#7fd1ab]"
    case "cancelled": return "text-muted-foreground line-through"
    default: return ""
  }
}

function short(value: string): string {
  return value.slice(5).replace("-", "/")
}

export function dateRangeLabel(start: string | null, due: string | null): string {
  if (start && due) return start === due ? short(due) : `${short(start)} – ${short(due)}`
  if (due) return `截止 ${short(due)}`
  if (start) return `${short(start)} 开始`
  return ""
}

export type PlanSpan = { start: string; end: string }

/** 计划时间轴:所有事项开始/截止的最早与最晚日期;没有任何日期返回 null。 */
export function planSpan(items: Pick<WorkItemSummary, "start_date" | "due_date">[]): PlanSpan | null {
  const dates = items.flatMap((item) => [item.start_date, item.due_date]).filter((value): value is string => Boolean(value)).sort()
  return dates.length ? { start: dates[0], end: dates[dates.length - 1] } : null
}

function dayNumber(value: string): number {
  return Math.round(Date.parse(`${value}T00:00:00Z`) / 86_400_000)
}

/** 甘特条位置(百分比);首尾按整天计,只有一个日期的事项画成一天宽。 */
export function barPosition(
  item: Pick<WorkItemSummary, "start_date" | "due_date">, span: PlanSpan,
): { left: number; width: number } | null {
  const first = item.start_date || item.due_date
  const last = item.due_date || item.start_date
  if (!first || !last) return null
  const total = dayNumber(span.end) - dayNumber(span.start) + 1
  const left = (dayNumber(first) - dayNumber(span.start)) / total
  const width = (dayNumber(last) - dayNumber(first) + 1) / total
  return { left: left * 100, width: width * 100 }
}

export function markerPosition(day: string, span: PlanSpan): number | null {
  const total = dayNumber(span.end) - dayNumber(span.start) + 1
  const offset = dayNumber(day) - dayNumber(span.start)
  if (offset < 0 || offset >= total) return null
  return ((offset + 0.5) / total) * 100
}

export function groupBuckets(entries: WeeklyBucketEntry[]): Record<WeeklyBucket, WeeklyBucketEntry[]> {
  const groups: Record<WeeklyBucket, WeeklyBucketEntry[]> = { completed: [], in_progress: [], planned: [], open: [] }
  for (const entry of entries) groups[entry.bucket].push(entry)
  return groups
}

function display(field: string, value: unknown): string {
  if (value === null || value === undefined || value === "") return "空"
  if (field === "status") return STATUS_LABELS[value as WorkItemStatus] || String(value)
  if (field === "priority") return PRIORITY_LABELS[value as WorkItemPriority] || String(value)
  if (Array.isArray(value)) return value.join("、") || "空"
  return String(value)
}

export function historyLabel(event: WorkItemHistoryEvent): string {
  const changes = Object.entries(event.changes || {}).map(([field, [before, after]]) => {
    if (field === "link") return after ? `挂载 ${display(field, after)}` : `解除 ${display(field, before)}`
    if (field === "description") return "修改描述"
    return `${FIELD_LABELS[field] || field} ${display(field, before)} → ${display(field, after)}`
  })
  return changes.length ? changes.join("；") : ACTION_LABELS[event.action] || event.action
}

export function worklogRef(date: string, index: number): string {
  return `${date}#${index}`
}
