export type WorkItemStatus = "todo" | "in_progress" | "waiting" | "done" | "cancelled"
export type WorkItemPriority = "high" | "normal" | "low"
export type WorkItemSource = "manual" | "alert" | "channel" | "plan"
export type WorkItemKind = "task" | "acceptance"
export type WorkItemLinkKind = "task" | "troubleshoot" | "worklog"

export type WorkItemLink = {
  kind: WorkItemLinkKind
  ref: string
  linked_at: string
  linked_by: string | null
}

export type WorkItemHistoryEvent = {
  ts: string
  by: string | null
  action: "created" | "imported" | "updated" | "status" | "linked" | "unlinked"
  changes: Record<string, [unknown, unknown]>
  note?: string
}

export type WorkItem = {
  id: string
  title: string
  description: string | null
  source: WorkItemSource
  source_ref: string | null
  status: WorkItemStatus
  assignee: string | null
  priority: WorkItemPriority
  kind: WorkItemKind
  plan_id: string | null
  plan_node: string | null
  start_date: string | null
  due_date: string | null
  created_by: string | null
  created_at: string
  updated_at: string
  closed_at: string | null
  links: WorkItemLink[]
  history: WorkItemHistoryEvent[]
}

export type WorkItemSummary = Omit<WorkItem, "links" | "history"> & { link_count: number }

export type WorkItemPatch = Partial<Pick<WorkItem,
  "title" | "description" | "status" | "assignee" | "priority" | "start_date" | "due_date" | "plan_id" | "plan_node"
>> & { note?: string }

export type WriteOperationSummary = {
  id: string
  kind: string
  title: string | null
  outcome: string | null
  approver: string | null
  decided_at: string | null
  oplog: string | null
}

export type RunSummary = {
  run_id: string
  missing?: boolean
  trigger?: string | null
  verdict?: string | null
  report_ref?: string | null
  artifacts?: string[]
  write_operations?: WriteOperationSummary[]
}

export type LinkedTask = RunSummary & {
  link: WorkItemLink
  task_id: string
  missing?: boolean
  title?: string
  status?: string
  task_type?: string | null
  created_at?: string
  created_by?: string | null
  approvals?: { approval_id: string; kind?: string; decision?: string; decided_at?: string; decided_by?: string | null }[]
}

export type LinkedWorklog = {
  ref: string
  derived: boolean
  missing?: boolean
  status?: string
  category?: string
  title?: string
  source?: string
  author?: string | null
}

export type WorkItemPlan = {
  id: string
  title: string
  description: string | null
  nodes: string[]
  created_by: string | null
  created_at: string
  updated_at: string
}

export type PlanProgress = {
  total: number
  done: number
  overdue: number
  by_status: Record<WorkItemStatus, number>
}

export type WorkItemPlanSummary = WorkItemPlan & { progress: PlanProgress }

export type WorkItemPlanDetail = {
  today: string
  plan: WorkItemPlan
  progress: PlanProgress
  nodes: { name: string; progress: PlanProgress; items: WorkItemSummary[] }[]
  unassigned: WorkItemSummary[]
}

export type WorkItemDetailResponse = {
  today: string
  item: WorkItem
  plan: WorkItemPlan | null
  links: { tasks: LinkedTask[]; troubleshoot: (RunSummary & { link: WorkItemLink })[]; worklogs: LinkedWorklog[] }
}

export type WorklogWorkItemRef = { id: string; title: string; status: WorkItemStatus; derived: boolean }

export type WeeklyBucket = "completed" | "in_progress" | "planned" | "open"

export type WeeklyBucketEntry = {
  item: WorkItemSummary
  bucket: WeeklyBucket
  status: WorkItemStatus
  in_next_period: boolean
  overdue: boolean
  worklog_refs: string[]
}

export type WeeklyBucketsResponse = {
  start_date: string
  end_date: string
  next_start_date: string
  next_end_date: string
  items: WeeklyBucketEntry[]
}

export type MatchSuggestion = {
  worklog_ref: string
  work_item_id: string
  work_item_title: string
  reason: string
}

export type ImportRow = {
  action: "create" | "update" | "unchanged"
  line: number
  node: string | null
  kind: WorkItemKind
  title: string
  start_date: string | null
  due_date: string | null
  done: boolean
  item_id: string | null
}

export type ImportResult = {
  has_nodes: boolean
  plan: (Partial<WorkItemPlan> & { title: string; nodes: string[] }) | null
  create: number
  update: number
  unchanged: number
  rows: ImportRow[]
}
