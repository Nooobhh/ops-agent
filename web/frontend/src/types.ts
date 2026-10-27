export type Consistency = { ok: boolean; level?: "warning" | "info"; detail: string | null }

export type PollSourceStatus = {
  last_result: "success" | "failure"
  consecutive_failures: number
  last_error_at: string | null
  last_error: string | null
}

export type PollStatus = {
  enabled: boolean
  effective: boolean
  runtime_state: "starting" | "running" | "stalled" | "stopped"
  reason: string | null
  updated_at: string | null
  since_boot: boolean
  last_tick_at: string | null
  sources: Record<string, PollSourceStatus>
}

export type NotifyChannelStatus = {
  enabled: boolean
  configured: boolean
  hint: string | null
  credential_masked: string | null
  credential_plain?: string | null
  secret_plain?: string | null
  updated_at: string | null
}

export type NotifyChannelsResponse = {
  wecom: NotifyChannelStatus
  feishu: NotifyChannelStatus & { signed: boolean }
}

export type NotifyProbeResponse = {
  ok: boolean
  code: number | null
  msg: string
}

export type NotifyTemplateVariable = {
  name: string
  label: string
  description: string
}

export type NotifyTemplateSettings = {
  format: "markdown"
  template: string
  effective_template: string
  default_template: string
  variables: NotifyTemplateVariable[]
  updated_at: string | null
}

export type CredentialLastTest = {
  status: "success" | "failure"
  error_code?: string | null
  tested_at: string
}

export type CredentialFieldSpec = {
  name: string
  label: string
  kind: "text" | "secret" | "number"
  required: boolean
  help: string
}

export type CredentialProfileStatus = {
  configured: boolean
  username_hint: string | null
  last_test: CredentialLastTest | null
}

/** 账号页凭据项:有哪些、字段与能否测试由环境包声明,密钥只写不回显。 */
export type CredentialProfile = CredentialProfileStatus & {
  id: string
  title: string
  description: string
  fields: CredentialFieldSpec[]
  testable: boolean
}

export type SshCredentialNodeStatus = {
  host: string
  target: string
  cached_at: string
  mode: 0 | 1 | null
  valid_from: number
  valid_to: number
  state: "expired" | "long_lived" | "active" | "unknown"
}

export type NodeCredentialProvider = {
  title: string
  description: string
  generate_label: string
  generate_confirm: string
}

export type NodeCredentialsStatus = {
  provider: NodeCredentialProvider | null
  nodes: SshCredentialNodeStatus[]
}

export type CredentialsResponse = {
  schema_version: 2
  profiles: CredentialProfile[]
  node_credentials: NodeCredentialsStatus
  ssh_test_platforms: Array<{ key: string; label: string }>
}

export type CredentialProfileInput = Record<string, string>

export type CredentialTestResponse = {
  schema_version: 1
  ok: boolean
  kind: "profile" | "ssh"
  profile_id?: string
  platform?: string
  error_code?: string
  message: string
  tested_at: string
}

export type NodeCredentialGenerateResponse = {
  schema_version: 1
  ok: boolean
  generated: string[]
  failed: Array<{ ip: string; error: string }>
  status: NodeCredentialsStatus
}

export type SshTestInput = {
  platform: string
  node: string
}

export type ToolCatalogItem = {
  name: string
  version: string
  category: string
  visibility: string
  allowed_agents: string[]
  allowed_sources: string[]
  allowed_phases: string[]
  side_effect: string
  transport: string
  target_kind: string
  requires: string[]
  timeout_seconds: number | null
  max_output_bytes: number | null
  retry_policy: Record<string, unknown>
  budget_cost: Record<string, unknown>
  approval_policy: string
  evidence_policy: string
  failure_policy: Record<string, unknown>
  failure_scope?: string
  continue_on_nonzero_exit?: boolean
  requires_success_of?: string[]
  idempotency: string
  description: string
  parameters: Record<string, unknown>
}

export type ToolCatalogResponse = {
  schema_version: 1
  items: ToolCatalogItem[]
}

export type EnvCluster = {
  key: string
  display_name: string
  short_name: string
  roles: string[]
}

export type EnvInfo = {
  schema_version: 1
  id: string
  display_name: string
  timezone: string
  // example = 未挂载环境数据,用的是程序自带的示例包;data_root = <数据根>/env;configured = paths.local.yaml 的 env_pack
  origin: "example" | "data_root" | "configured"
  path: string
  commit: string | null
  clusters: EnvCluster[]
}

export type HealthResponse = {
  status: "ok"
  data_sources: Record<string, boolean>
  timeline: { status: "ok" | "unavailable" | "recovering"; detail: string | null }
  consistency: Consistency
}

export type AcquisitionSettings = {
  schema_version: 1
  timezone: string
  alerts: {
    enabled: boolean
    interval_seconds: number
    history_root: string
    retention_days: number
  }
  resources: {
    enabled: boolean
    interval_seconds: number
    snapshot_root: string
    retention_days: number
    compression: "gzip" | "none"
    max_backfill_minutes: number
    risk_notifications_enabled: boolean
  }
  maintenance: { enabled: boolean; hour: number; minute: number }
}

export type AcquisitionResponse = {
  settings: AcquisitionSettings
  paths: Record<"alerts" | "resources", {
    configured: string
    resolved: string
    exists: boolean
    writable: boolean
    available_bytes: number | null
  }>
  storage: Record<"alerts" | "resources", {
    file_count: number
    total_bytes: number
    oldest: string | null
    newest: string | null
  }>
  resource_notifications: {
    enabled: boolean
    active_count: number
    notified_count: number
    pending_count: number
    updated_at: string | null
  }
  runtime: { jobs?: Record<string, Record<string, unknown>>; updated_at?: string }
}

export type ResourceAlertField = {
  key: string
  label: string
  unit: string
  description: string
  default: number | null
  override: number | null
  effective: number | null
  path: string[]
}

export type ResourceAlertSettingsGroup = {
  key: string
  kind: "resource" | "business"
  label: string
  description: string
  unit?: string
  metric?: string
  fields: ResourceAlertField[]
}

export type ResourceAlertSettingsResponse = {
  schema_version: 1
  groups: ResourceAlertSettingsGroup[]
  overrides: Record<string, unknown>
}

export type ResourceFilesystem = {
  device: string
  mountpoint: string
  fstype: string
  kind: "local" | "static" | "shared"
  used_pct: number | null
  inode_used_pct: number | null
  available_bytes: number | null
  size_bytes: number | null
  files?: number | null
  files_free?: number | null
}

export type ResourceNode = {
  node: string
  platform: string
  instance: string
  sample_at: string | null
  status: string
  metrics: Record<string, number | null>
  filesystems: ResourceFilesystem[]
}

export type ResourceService = {
  node: string
  platform: string
  service: string
  component: string
  job: string
  role: string | null
  profile: string
  status: string
  sample_at: string | null
  metrics: Record<string, number | null>
  gc: Record<string, Record<string, number>>
  old_gen_after_gc_bytes: Record<string, number>
}

export type ResourceBusinessMetric = {
  platform: string
  node: string
  service: string
  job: string
  metric: string
  label: string
  unit: "seconds" | "percent" | "per_second" | "count" | "number" | string
  value: number | null
  status: "ok" | "target_down" | "no_data" | "source_error" | string
  severity: "healthy" | "warning" | "critical" | "unknown" | string
  sample_at: string | null
  details: Record<string, string>
}

export type ResourceChartTarget = {
  kind: "node" | "service" | "business"
  platform: string
  node: string
  title: string
  job?: string
  role?: string | null
  service?: string
}

export type ResourceSnapshot = {
  snapshot_id: string
  collected_at: string
  platform: string
  status: "ok" | "partial" | "error"
  coverage: {
    expected_nodes: number
    observed_nodes: number
    expected_services: number
    observed_services: number
  }
  nodes: ResourceNode[]
  services: ResourceService[]
  business_metrics?: ResourceBusinessMetric[]
  missing: Array<Record<string, string | null>>
  errors: Array<Record<string, unknown>>
}

export type ResourcesLatestResponse = {
  generated_at: string
  items: ResourceSnapshot[]
  catalog: {
    updated_at: string | null
    services: Array<{
      platform: string
      node: string
      service: string
      component: string
      job: string
      profile: string
      role?: string | null
      status: string
    }>
    unmonitored_components: Array<Record<string, unknown>>
  }
}

export type ResourceHistoryPoint = {
  collected_at: string
  status: string
  nodes: ResourceNode[]
  services: ResourceService[]
  business_metrics: ResourceBusinessMetric[]
}

export type ResourceHistoryResponse = {
  platform: string
  node: string | null
  hours: number
  points: ResourceHistoryPoint[]
}

export type ResourceRisksResponse = {
  generated_at: string | null
  snapshot_count: number
  forecast_status?: Array<{
    platform: string
    status: "no_data" | "collecting" | "ready"
    required_observed_hours: number
    observed_hours: number
    series_count: number
    ready_series_count: number
    span_hours: number
    first_collected_at: string | null
    last_updated_at: string | null
    has_data_gaps: boolean
    largest_gap_seconds: number
    warning_eta_hours: number
    finding_count: number
    health?: "healthy" | "warning" | "critical"
  }>
  findings: Array<{
    key: string
    severity: "watch" | "warning" | "critical"
    platform: string
    node: string
    resource: string
    alert_type?: "current" | "forecast"
    title?: string
    description?: string
    duration_seconds?: number
    last_updated_at?: string | null
    evidence: Record<string, unknown>
  }>
}

export type ResourceSummaryResponse = {
  generated_at: string
  enabled: boolean
  interval_seconds: number
  runtime_state: "starting" | "running" | "stalled" | "stopped"
  last_collected_at: string | null
  last_success_at: string | null
  consecutive_failures: number
  last_error: string | null
  platforms: Array<{
    platform: string
    status: "ok" | "partial" | "error"
    collected_at: string | null
    expected_nodes: number
    observed_nodes: number
    expected_services: number
    observed_services: number
    error_count: number
  }>
  stats: {
    expected_nodes: number
    observed_nodes: number
    expected_services: number
    observed_services: number
    local_mounts: number
    risk_count: number
    snapshot_count: number
  }
  risk_severity: { critical: number; warning: number; watch: number }
}

export type AppShellOutletContext = {
  poll: PollStatus | null
  resourceSummary: ResourceSummaryResponse | null
  pollBusy: boolean
  toggleAlertPoll: () => Promise<void>
}

export type TimelineEvent = {
  event_id: string
  ts: string
  type: string
  key: string
  source: string
  platform: string
  alertname: string
  host: string | null
  alert_class: string | null
  detail: unknown
  suppressed_by: string | null
  severity: string | null
  service: string | null
}

export type AlertClass = "watch" | "chronic" | "ignore" | "flapping"

export type AlertSummary = {
  key: string
  source: string
  platform: string
  alertname: string
  host: string | null
  status: "active" | "resolved" | "suppressed"
  chronic: boolean
  severity: string | null
  muted: boolean
  service: string | null
  class: AlertClass
  effective_class: AlertClass
  first_seen: string | null
  last_seen: string | null
  occurrences: number
  triage: Record<string, unknown> | null
  suppressed_by: string | null
  latest_event: TimelineEvent | null
}

export type DistributionBucket = {
  bucket: string
  count: number
  by_severity: Record<string, number>
}

export type AlertChange = {
  key: string
  source: string
  platform: string
  alertname: string
  host: string | null
  severity: string | null
  service: string | null
  change_type: "new" | "resolved" | "escalated"
  changed_at: string
  change_count: number
  occurrence_count: number
  status: "active" | "resolved" | "suppressed"
  chronic: boolean
  muted: boolean
  class: AlertClass
  effective_class: AlertClass
  first_seen: string | null
}

export type ChangesBlock = {
  range: string
  total: number
  items: AlertChange[]
}

export type FacetsResponse = Record<
  "source" | "platform" | "severity" | "service",
  string[]
>

export type OverviewResponse = {
  generated_at: string
  // Optional for compatibility with a web process that has not been restarted
  // after the credential warning response field was introduced.
  credential_warnings?: {
    available: boolean
    threshold_days: number
    count: number
    expired_count: number
    items: Array<{
      host: string
      target: string
      valid_to: number
      state: "expired" | "expiring"
      remaining_days: number
    }>
  }
  stats: {
    active: number
    active_non_chronic: number
    active_non_chronic_class: number
    new_today: number
    resolved_today: number
    suppressed_today: number
    chronic: number
    chronic_class: number
    ignore_class: number
  }
  distribution: DistributionBucket[]
  changes: ChangesBlock
  consistency: Consistency
}

export type AlertListResponse = {
  items: AlertSummary[]
  total: number
  limit: number
  offset: number
  filter_notice: string | null
}

export type Occurrence = {
  started_at: string
  resolved_at: string | null
  duration_seconds: number
  open: boolean
  started_from?: "source"
}

export type Peer = {
  key: string
  source: string
  relation: "suppressed_by" | "suppresses"
  payload: Record<string, unknown> | null
  latest_event: TimelineEvent | null
}

export type AlertDetailResponse = {
  key: string
  source: string
  // 规则级覆盖与来源开关用的源 id(后端按告警源配置推导)
  source_id?: string
  platform: string
  alertname: string
  host: string | null
  description: string | null
  current: {
    status: "active" | "resolved"
    severity: string | null
    muted: boolean
    service: string | null
    first_seen: string | null
    last_seen: string | null
    occurrences: number
    class: AlertClass
    effective_class: AlertClass
    triage: Record<string, unknown> | null
    source_active_at: string | null
  }
  payload: { available: boolean; source_of: string | null; data: Record<string, unknown> | null }
  occurrences: Occurrence[]
  events: TimelineEvent[]
  routing: {
    doc: string; doc_title: string; doc_url: string | null; playbook: string | null; matched_via: string
  } | null
  dedup: {
    live: { suppressed_by: string | null; suppresses: string[]; excluded_sources: string[] }
    historical: { suppressed_by: string | null; incomplete: true }
    rule: { winner: string; reason: string } | null
  }
  peers: Peer[]
}

/** 告警源 id:全局源为类型,按集群的扩展源为「类型:集群」,随告警源配置 */
export type AlertConfigSourceId = string

export type AlertConfigAlert = {
  alertname: string
  severity?: string | null
  severityOverride: "critical" | "warning" | "info" | null
  class: string
  notify: boolean
  platform?: string
  group?: string
  state?: string
  label?: string
  enabled?: boolean
  service?: string
}

export type AlertConfigSource = {
  updated_at: string | null
  alerts: AlertConfigAlert[]
}

// 来源级中台推送开关:关了只是不推送,照常接收、记时间线与自动排查
export type AlertSourceNotify = {
  notify: boolean
  default: boolean
  overridden: boolean
}

export type AlertConfigResponse = {
  sources: Partial<Record<AlertConfigSourceId, AlertConfigSource>>
  catalog_available: boolean
  source_notify: Partial<Record<AlertConfigSourceId, AlertSourceNotify>>
  sources_meta: Array<{ id: AlertConfigSourceId; label: string }>
}

export type TriageTaskStatus =
  | "queued"
  | "running"
  | "waiting_input"
  | "waiting_approval"
  | "waiting_operation"
  | "completed"
  | "failed"
  | "kb_failed"
  | "cancelled"

export type TriageTaskSource = "manual" | "auto"
/** 实时目标 = 环境包声明的集群旧短键 */
export type TriageTarget = string

export type TaskType = "qa" | "investigation" | "alert" | "artifact" | "default"

export type AlertTaskContext = {
  schema_version?: 1
  source: string
  fingerprint?: string | null
  alertname: string
  labels?: Record<string, unknown>
  annotations?: Record<string, unknown>
  platform?: string | null
  host_raw?: string | null
  node?: string | null
  service?: string | null
  severity?: string | null
  state?: string | null
  active_at?: string | null
  value?: string | null
  detail?: string | null
  raw_ref?: string | null
}

export type ReasoningEffort = "none" | "low" | "medium" | "high" | "xhigh"
// Harness 档位:A3.7 起新任务一律 autonomous;guided 为评估期遗留,空/structured 为旧版只读任务
export type HarnessTier = "autonomous" | "guided" | "structured"
export type CommandMode = "whitelist" | "auto" | "bypass"

export type ModelOption = {
  id: string
  title: string
  base_url: string
  model: string
  api_key_configured: boolean
  default_reasoning_effort: ReasoningEffort
}

export type WorkflowModelDefault = {
  model_id: string
  reasoning_effort: ReasoningEffort
  timeout_seconds: number
}

export type AgentTypePolicy = {
  model_id: string
  reasoning_effort: ReasoningEffort
  max_tool_turns: number
  model_timeout_seconds: number
}

export type ModelSettingsResponse = {
  schema_version: 1
  models: ModelOption[]
  defaults: {
    auto: WorkflowModelDefault
    manual: WorkflowModelDefault
    weekly: WorkflowModelDefault
    reviewer?: WorkflowModelDefault | null
  } | null
  agent_policies?: Record<TaskType, AgentTypePolicy>
  error: string | null
}

export type TriageTask = {
  schema_version: 1
  task_id: string
  execution_id?: string | null
  source: TriageTaskSource
  status: TriageTaskStatus
  created_at: string
  updated_at: string
  initial_message: string
  display_title?: string | null
  intent?: string | null
  platform: TriageTarget | null
  node: string | null
  service?: string | null
  component?: string | null
  context_missing?: string[]
  scope: "node" | "cluster" | null
  alert_key: string | null
  occurrence_key: string | null
  alert_event: Record<string, unknown> | null
  task_type?: TaskType | null
  task_type_confidence?: number | null
  task_type_source?: "structured" | "rule" | "model" | "user_confirmed" | null
  classifier_version?: string | null
  classification_audit?: Record<string, unknown> | null
  polling_snapshot?: Record<string, unknown> | null
  risk_seed?: Record<string, unknown> | null
  agent_id?: string | null
  agent_prompt_version?: string | null
  agent_policy_snapshot?: AgentTypePolicy | null
  alert_context?: AlertTaskContext | null
  active_agent?: string | null
  agent_path?: string[]
  context_pack_ref?: string | null
  context_pack_snapshot?: Record<string, unknown> | null
  loop_phase?: string | null
  loop_stop_reason?: string | null
  current_hypothesis?: Record<string, unknown> | null
  hypothesis_history?: Array<Record<string, unknown>>
  artifact_refs?: string[]
  model_profile_id: string | null
  model_title: string | null
  model_name: string | null
  reasoning_effort: ReasoningEffort | null
  harness_tier?: HarnessTier | null
  command_mode: CommandMode | null
  manual_artifacts?: boolean
  run_id: string | null
  matched_doc: string | null
  consulted_docs: string[]
  report_path: string | null
  kb_doc: string | null
  kb_declined: boolean
  worklog_cursor: string | null
  worklog_entries: number
  deep_dive: boolean
  tool_turns: number
  diagnostic_calls: number
  tool_call_counts?: Record<string, number>
  tool_target_counts?: Record<string, number>
  tool_target_runtime_seconds?: Record<string, number>
  tool_runtime_seconds?: number
  tool_output_bytes?: number
  budget_grants: number
  error: string | null
  failure_kind: "knowledge" | "rejections" | "turn_cap" | "time_limit" | "diagnostic_cap" | "tool_cap" | "model_unavailable" | "closing_round_unanswered" | "legacy_runtime_retired" | null
  lease_owner: string | null
  lease_expires_at: string | null
  pending_invocation_id: string | null
  archived_at: string | null
}

export type TriageEvent = {
  schema_version: 1
  event_id: string
  task_id: string
  ts: string
  type:
    | "user_message"
    | "assistant_message"
    | "tool_call"
    | "tool_result"
    | "tool_rejection_chain"
    | "approval_requested"
    | "approval_decided"
    | "task_type_decided"
    | "task_type_clarification_requested"
    | "task_type_confirmed"
    | "task_context_clarification_requested"
    | "task_context_confirmed"
    | "model_retry"
    | "harness_feedback"
    | "agent_selected"
    | "context_pack_built"
    | "agent_playbook_result"
    | "hypothesis_proposed"
    | "evidence_plan_created"
    | "hypothesis_reviewed"
    | "agent_handoff_requested"
    | "agent_handoff_decided"
    | "agent_handoff_accepted"
    | "agent_handoff_completed"
    | "loop_finished"
    | "agent_decision"
    | "agent_opening"
    | "harness_notice"
    | "model_usage"
    | "budget_reminder"
    | "closing_round"
    | "closing_round_unanswered"
    | "harness_tier_changed"
    | "operation_executing"
    | "operation_executed"
    | "operation_submitted"
    | "operation_progress"
    | "operation_finished"
    | "status"
    | "error"
  payload: Record<string, unknown>
}

export type ToolResultOutcome =
  | "completed"
  | "completed_partial"
  | "denied"
  | "needs_input"
  | "pending_approval"
  | "failed"
  | "skipped"
  | "replayed"
  | string

export type ToolResultEnvelope = {
  schema_version: 1
  tool: {name: string; version: string}
  call_id: string | null
  ok: boolean
  outcome: ToolResultOutcome
  execution_state?: "executed" | "not_executed" | "unknown" | string
  command_status?: "zero" | "nonzero" | "timeout" | "truncated" | "unknown" | "not_applicable" | string
  skip?: {reason?: string; [key: string]: unknown} | null
  dependency?: Record<string, unknown>
  timing?: {
    admission_ms: number
    dependency_resolution_ms: number
    dispatch_ms: number
    remote_exec_ms: number
    session_write_ms: number
    report_write_ms: number
    task_store_ms: number
    event_write_ms: number
    total_ms: number
  }
  target: {
    kind: string
    platform?: string | null
    node?: string | null
    scope?: string | null
  }
  data: Record<string, unknown>
  connection: Record<string, unknown> | null
  evidence: {eligible: boolean; refs: string[]; provenance: string}
  metrics: {duration_ms: number; output_bytes: number; attempts?: number}
  warnings: string[]
  error: Record<string, unknown> | null
}

export type TriageTaskDetail = {
  schema_version: 1
  task: TriageTask
  events: TriageEvent[]
}

export type TriageTaskListResponse = {
  schema_version: 1
  items: TriageTask[]
}

export type TriageTaskEventsResponse = {
  schema_version: 1
  items: TriageEvent[]
}

export type TaskArtifact = {
  filename: string
  type: "md" | "sh" | "py"
  size: number
  created_at: number
}

export type TaskArtifactListResponse = {
  schema_version: 1
  items: TaskArtifact[]
}

export type TriageHealth = {
  schema_version: 1
  model: "configured" | "not_configured"
  can_create: boolean
  worker: {
    state: "starting" | "running" | "idle" | "stopped" | "not_configured"
    pid: number | null
  }
  queue: Record<"queued" | "running" | "waiting_input" | "waiting_approval" | "waiting_operation", number>
}

export type CreateTriageTaskRequest = {
  message: string
  platform?: TriageTarget
  node?: string
  service?: string
  component?: string
  model_id?: string
  reasoning_effort?: ReasoningEffort
  command_mode?: CommandMode
  manual_artifacts?: boolean
  alert_context?: AlertTaskContext
  task_type?: TaskType
}

export type ResourceRiskTaskRequest = {
  message?: string
  model_id?: string
  reasoning_effort?: ReasoningEffort
  command_mode?: CommandMode
}

export type RuntimeControlUpdates = Partial<{
  platform: TriageTarget | null
  node: string | null
  model_id: string | null
  reasoning_effort: ReasoningEffort | null
  command_mode: CommandMode | null
}>

export type CommandPolicyResponse = {
  mode: CommandMode
  allow: string[]
  deny: string[]
}

export type ApprovalRequest = {
  approvalId: string
  actionId: string
  command: string
  impact: string
  rollback: string
}

export type ApprovalDecision = "approve" | "revise" | "reject"

export type ApprovalScope = "once" | "task" | "always"

export type CommandReviewTargets = {
  paths: string[]
  mysql: Array<{database: string | null; tables: string[]}>
  mysql_parsed: boolean
}

export type CommandReviewVerdict = {
  safe: boolean
  reason: string
  reviewed: boolean
}

export type KbReviewRequest = {
  approvalId: string
  doc: string
  mode: string
  markdown: string
  diff: string
  evidenceIssues?: string[]
}

export type KbProposalRequest = {
  approvalId: string
  doc: string
  mode: string
  title: string
  summary: string
  symptoms: string[]
}

export type WorklogReviewRequest = {
  approvalId: string
  preview: string
}

export type BudgetReviewRequest = {
  approvalId: string
  reason: string
  toolTurns: number
  turnBudget: number
  diagnosticCalls: number
  diagnosticBudget: number
  grants: number
  grantTurns: number
  hasReport: boolean
}

export type KbReviewOutcome = {
  decision: ApprovalDecision
  feedback: string
  scope?: ApprovalScope
}

/** 插件写操作的审批请求:预检计划(Harness 从插件取得)+ 影响面;不含令牌与密钥。 */
export type OperationReviewRequest = {
  approvalId: string
  title: string
  plugin: string
  cluster: string
  planId: string
  planHash: string
  reason: string
  createdAt: string
  expiresAt: string
  expectedMaxDurationS: number
  summary: string[]
  current: Array<{host: string; component: string; state: string; desiredState: string; maintenance: string}>
  pendingChanges: string[]
  warnings: string[]
  request: {method: string; path: string; body: string}
  impact: {ok: boolean; downstream: string[]; upstream: string[]; error: string} | null
  replannedFrom: string | null
  replanReason: string
}

export type OperationOutcome =
  | "succeeded" | "partial" | "failed" | "unverified" | "unknown" | "noop" | "not_executed"

/** 批准后 Harness 执行与跟踪的进展(由 operation_* 事件归并)。 */
export type OperationExecution = {
  phase: "executing" | "reconciling" | "submitted" | "finished"
  requestId: number | null
  requestStatus: string | null
  progressPct: number | null
  tasks: Array<{host: string; component: string; status: string; exitCode: number | null; durationS: number | null}>
  outcome: OperationOutcome | null
  reason: string
  oplog: string | null
  oplogError: string | null
  replanned: boolean
  tokenDigest: string | null
}

export type ReportGateRequest = {
  approvalId: string
  reason: string
  verdict: string
  severity: string
  rootCause: string
  evidence: string[]
  remediation: string[]
  // 新运行时:finish 的回答与每个猜测的核验结果(旧任务没有)
  answer?: string
  hypotheses?: FinishHypothesis[]
}

export type FinishStatus = "supported" | "insufficient" | "refuted"

export type FinishEvidence = {
  role: "observation" | "criterion" | "absence" | "related" | "fact" | string
  ref: string
  found: boolean
  text: string
  reason: string
}

export type FinishHypothesis = {
  statement: string
  status: FinishStatus
  declared: FinishStatus | null
  reasons: string[]
  evidence: FinishEvidence[]
  related: FinishEvidence[]
}

export type FinishSummary = {
  verdict: string
  grounded: boolean
  hypotheses: FinishHypothesis[]
  facts: FinishEvidence[]
  report: string | null
}

export type TaskTypeQuestionRequest = {
  question: string
  options: string[]
  taskType: string | null
}

export type WikiLink = {
  target: string
  label: string
  resolved_path: string | null
  ambiguous: boolean
  href?: string | null
}

export type KnowledgeIndexState = {
  available: boolean
  built_at: string | null
  source_latest_at: string | null
  stale: boolean
  hint: string | null
}

/** 运维视角 5 类文档(后端 lib/kb/categories.py 推导) */
export type KnowledgeCategory = "troubleshoot" | "procedure" | "cluster_info" | "reference" | "oplog"

export type KnowledgeTreeDoc = {
  path: string
  title: string
  doc_title: string | null
  display_title: string
  component: string | null
  doc_type: string
  status: string
  problem: string | null
  alerts: string[]
  category: KnowledgeCategory
  sop_kind: string | null
}

export type KnowledgeServiceNode = {
  service: string
  index_doc: string | null
  index_title: string | null
  component_indexes: {
    component: string
    index_doc: string
    index_title: string | null
  }[]
  docs: KnowledgeTreeDoc[]
}

export type KnowledgePlatformNode = {
  platform: string
  services: KnowledgeServiceNode[]
}

/** 知识库根下的一个库(目录 + kb.yaml);旧单库布局 id 为空串 */
export type KnowledgeBaseNode = {
  id: string
  name: string
  description: string
  layout: "platform_service" | "flat"
  platforms: KnowledgePlatformNode[]
}

export type KnowledgeTree = {
  index_state: KnowledgeIndexState
  bases: KnowledgeBaseNode[]
  warnings: string[]
  /** 平台键 → 显示名,按环境包声明顺序 */
  platform_labels?: Record<string, string>
  service_labels?: Record<string, string>
}

export type KnowledgeTopologyProvenance = {
  kind: string
  path: string
  collected_at?: string
}

export type KnowledgeTopologyNode = {
  id: string
  kind: string
  name: string
  platform: string | null
  properties: Record<string, unknown>
  provenance: KnowledgeTopologyProvenance[]
}

export type KnowledgeTopologyEdge = {
  id: string
  source: string
  target: string
  relation: string
  properties: Record<string, unknown>
  provenance: KnowledgeTopologyProvenance[]
}

export type KnowledgeTopology = {
  schema_version: number
  topology_kind?: string
  platform?: string
  generated_at?: string
  completed_at?: string
  update_mode?: "full" | "incremental" | string
  source_fingerprint: string
  nodes: KnowledgeTopologyNode[]
  edges: KnowledgeTopologyEdge[]
  warnings: string[]
  // 环境包图谱的视图元数据(lib/graphs);知识库拓扑没有
  view?: {
    core_kinds: string[]
    default_visible_kinds: string[]
    optional_kinds: string[]
    count_kind?: string
    count?: number
    summary_kinds?: string[]
  }
  coverage: {
    component_service_mapping?: {
      mapped: number
      unmapped: string[]
    }
    component_observation?: {
      component_entities: number
      observed_in_platform_config: number
      baseline_only: string[]
    }
    service_baseline?: {
      operational_service_count: number
      with_baseline: number
      without_baseline: string[]
    }
    service_dependencies?: {
      documented_edge_count: number
      config_declared_edge_count?: number
      config_relation_counts?: Record<string, number>
      services_with_evidence: string[]
      services_without_evidence: string[]
    }
  }
  stats: {
    node_count: number
    edge_count: number
    node_kinds: Record<string, number>
    edge_relations: Record<string, number>
    platforms: string[]
    clusters: number
    services: number
    warning_count: number
  }
}

export type KnowledgeDocMeta = {
  path: string
  title: string
  doc_title: string | null
  display_title: string
  platform: string | null
  service: string | null
  type: string | null
  status: string | null
  problem: string | null
  symptoms: string[]
  alerts: string[]
  playbook: string | null
  schema_version: number | null
  source: string | null
  source_ref: string | null
  summary: string | null
  verified_by: string | null
  verified_at: string | null
  verified_reason: string | null
  lint_error: string | null
  category?: KnowledgeCategory
  sop_kind?: string | null
}

export type KnowledgeDoc = {
  path: string
  meta: KnowledgeDocMeta
  frontmatter_raw: string
  markdown: string
  wikilinks: WikiLink[]
}

export type KnowledgeSearchHit = {
  path: string
  platform: string
  service: string
  doc_type: string
  status: string
  title: string
  display_title: string
  score: number
  snippet: string
  category: KnowledgeCategory
  sop_kind: string | null
}

export type KnowledgeSearchResult = {
  index_state: KnowledgeIndexState
  hits: KnowledgeSearchHit[]
}

export type KnowledgeReferenceTask = {
  task_id: string
  run_id: string | null
  status: string | null
  source: string | null
  platform: string | null
  created_at: string | null
  relations: string[]
}

export type KnowledgeReferenceConclusion = {
  run_id: string
  verdict: string | null
  severity: string | null
  kb_doc_effective: boolean | null
  via: "kb_doc" | "kb_draft"
}

export type KnowledgeCaseLine = {
  text: string
  run_id: string | null
}

export type KnowledgeReferences = {
  path: string
  tasks: KnowledgeReferenceTask[]
  alerts: string[]
  conclusions: KnowledgeReferenceConclusion[]
  case_lines: KnowledgeCaseLine[]
  coverage: {
    sessions_total: number
    sessions_linkable: number
    note: string
  }
}

export type KnowledgePlatformCoverage = {
  platform: string
  documents: number
  troubleshoot: number
  with_alerts: number
  draft: number
  verified: number
}

export type KnowledgeOrphanDoc = {
  path: string
  title: string
  platform: string
  service: string
}

export type KnowledgePromotionCandidate = {
  path: string
  title: string
  platform: string
  service: string
  eligible: boolean
  run_id: string | null
  attempts: KnowledgePromotionAttempt[]
}

export type KnowledgePromotionAttempt = {
  run_id: string
  via: string[]
  verdict: string | null
  aligned: boolean
  explicit_doc_reference: boolean
  matched_terms: string[]
  eligible: boolean
  reasons: string[]
}

export type KnowledgeHealth = {
  index: KnowledgeIndexState
  documents_total: number
  status_counts: { draft: number; verified: number; other: number }
  type_counts: Record<string, number>
  platform_coverage: KnowledgePlatformCoverage[]
  recall_orphans: {
    count: number
    troubleshoot_total: number
    documents: KnowledgeOrphanDoc[]
  }
  promotion_candidates: KnowledgePromotionCandidate[]
  promotion_reviews: KnowledgePromotionCandidate[]
}

export type WorklogEntry = {
  status: string
  category: string
  title: string
  source: string
  output: string
  related: string
  process: string[]
  entry_index: number
  weekly_report_excluded: boolean
  raw: string
}

export type WorklogWeeklyReportExclusionResponse = {
  schema_version: 1
  date: string
  entry_index: number
  weekly_report_excluded: boolean
}

export type WorklogDetailResponse = {
  schema_version: 1
  date: string
  entry: WorklogEntry
  report_path: string | null
  report_markdown: string | null
}

export type WorklogDay = {
  date: string
  weekday: string
  available: boolean
  entries: WorklogEntry[]
}

export type WorklogResponse = {
  schema_version: 1
  start_date: string
  end_date: string
  days: WorklogDay[]
}

export type WorklogCreateInput = {
  date: string
  status: "DONE" | "WIP" | "BLOCKED"
  category: "BUGFIX" | "FEATURE" | "RESEARCH" | "OPS" | "DOC" | "MEETING"
  title: string
  output: string
  related: string
  process: string[]
  work_item_id?: string
}

export type WorklogCreateResponse = {
  schema_version: 1
  date: string
  path: string
  entry: Omit<WorklogEntry, "entry_index" | "weekly_report_excluded">
}

export type WeeklyReportItem = {
  filename: string
  start_date: string
  end_date: string
  updated_at: string
}

export type WeeklyReportPreview = {
  start_date: string
  end_date: string
  filename: string
  summary: string[]
  todos: string[]
  days: WorklogDay[]
}

export type WeeklyReportCreateResponse = {
  schema_version: 1
  job_id: string
  operation: "create" | "regenerate"
  start_date: string
  end_date: string
  status: "queued" | "running" | "completed" | "failed"
  current_stage: string
  stage_status: "queued" | "running" | "completed" | "failed"
  completed_stages: string[]
  report: WeeklyReportItem | null
  preview: WeeklyReportPreview | null
  error: string | null
  created_at: string
  updated_at: string
}

export type WeeklyReportJob = WeeklyReportCreateResponse

export type WeeklyReportJobListResponse = {
  schema_version: 1
  items: WeeklyReportJob[]
}

export type WeeklyReportDeleteResponse = {
  schema_version: 1
  report: WeeklyReportItem
}

export type WeeklyReportListResponse = {
  schema_version: 1
  items: WeeklyReportItem[]
}

export type WeeklyReportDocumentPreview = {
  schema_version: 1
  report: WeeklyReportItem
  paragraphs: string[]
  customer: string
  contract_number: string
  project_name: string
  project_manager: string
  summary: string[]
  todos: string[]
}

export type WeeklyReportSettings = {
  schema_version: 1
  start_weekday: number
  end_weekday: number
  start_label: string
  end_label: string
  timezone: string
  current_start_date: string
  current_end_date: string
}

export type LocalUser = {
  id: string
  display_name: string
  inferred: boolean
}

export type LocalUserResponse = {
  schema_version: 1
  configured: boolean
  user: LocalUser | null
  error: string | null
}

export type VectorStoreBackend = "embedded" | "milvus" | "auto"
export type EmbeddingBackend = "disabled" | "local" | "external"
export type RerankBackend = "disabled" | "local" | "external"

export type VectorSettingsResponse = {
  schema_version: 1
  settings: {
    backend: VectorStoreBackend
    embedded_path: string
    fallback_to_embedded: boolean
    timeout_seconds: number
    milvus: {
      endpoint: string
      collection: string
      vector_field: string
      token_configured: boolean
    }
    embedding: {
      backend: EmbeddingBackend
      model: string
      endpoint: string
      api_key_configured: boolean
      dimensions: number | null
      device: string
    }
    rerank: {
      backend: RerankBackend
      model: string
      endpoint: string
      api_key_configured: boolean
      device: string
      max_candidates: number
      max_length: number
    }
  }
  status: {
    retrieval_index_available: boolean
    embedded_vector_available: boolean
    embedded_vector_count: number
    embedded_vector_dimension: number | null
    local_embedding_runtime_available: boolean
    embedding_model_configured: boolean
    embedding_model: string
    local_rerank_runtime_available: boolean
    rerank_model_configured: boolean
    rerank_model: string
    rerank_ready: boolean
    semantic_ready: boolean
    milvus_connection: "not_tested" | string
    config_file: string
  }
}

export type VectorSettingsInput = {
  schema_version: 1
  backend: VectorStoreBackend
  embedded_path: string
  fallback_to_embedded: boolean
  timeout_seconds: number
  milvus: {
    endpoint: string
    collection: string
    vector_field: string
    token?: string
  }
  embedding: {
    backend: EmbeddingBackend
    model: string
    endpoint: string
    api_key?: string
    dimensions: number | null
    device: string
  }
  rerank: {
    backend: RerankBackend
    model: string
    endpoint: string
    api_key?: string
    device: string
    max_candidates: number
    max_length: number
  }
}

export type VectorSettingsTestResponse = {
  ok: boolean
  backend: string
  message: string
}

export type PluginAuthScheme = "none" | "basic" | "bearer" | "header" | "connections"
export type PluginHealthState = "ok" | "unreachable" | "auth_failed" | "protocol_error" | "not_configured"

export type PluginWriteRole = "plan" | "execute" | "track" | "reconcile" | "impact"

export type PluginTool = {
  name: string
  read_only: boolean
  // 两阶段写里的角色(环境包声明);null 为普通工具
  write_role: PluginWriteRole | null
  hash: string
  enabled: boolean
  blocked_reason: string | null
  needs_review: boolean
  model_description: string
}

export type PluginView = {
  id: string
  title: string
  source: "official" | "custom"
  url: string
  default_url: string | null
  cluster: string | null
  auth: PluginAuthScheme
  header_name: string | null
  auth_account: string | null
  timeout_seconds: number
  enabled: boolean
  credential: CredentialProfileStatus
  version: string | null
  manifest: boolean
  fetched_at: string | null
  tools: PluginTool[]
  // 声明了两阶段写的官方插件;密钥只回是否配置
  write: { plan: string; execute: string; approval_secret_configured: boolean } | null
  // 全局插件:按哪个标签区分集群、哪些工具由 Harness 强制带本集群过滤
  scope: { label: string; clusters: Record<string, string>; tools: string[] } | null
  // 连接列表认证:已保存的连接(不回密码、账号打码);其他认证为 null
  connections: PluginConnection[] | null
  // 后台采集用到的类别(alerts / resources),不看插件开关
  feeds: string[]
}

export type PluginsResponse = {
  schema_version: 1
  plugins: PluginView[]
  clusters: Array<{ key: string; name: string }>
}

export type PluginHealth = {
  state: PluginHealthState
  detail: string | null
  version: string | null
  checked_at: string
  enabled: boolean
}

export type PluginHealthResponse = { schema_version: 1; plugins: Record<string, PluginHealth> }

export type PluginTestResponse = {
  ok: boolean
  message: string
  error_code: string | null
  manifest: boolean
  version: string | null
  tools: Array<{ name: string; read_only: boolean }>
  // 连接列表认证的逐条试连结果
  probe?: PluginProbe | null
  plugin?: PluginView
}

export type PluginConnection = {
  name: string
  host: string
  port: number
  user_hint: string
  database: string | null
  labels: Record<string, string>
  password_set: boolean
}

// 保存连接:账号 / 密码留空即沿用同名连接的原值
export type PluginConnectionInput = {
  name: string
  host: string
  port: number
  user?: string
  password?: string
  database?: string
  platform: string
}

export type PluginProbe = {
  ok: boolean
  connections: Array<Record<string, unknown>>
  failed: Array<{ connection: string; code: string }>
  warnings: string[]
  error: { code: string; message: string } | null
}

export type PluginCredentialInput = { user?: string; password?: string; token?: string; value?: string }

export type CustomPluginInput = {
  id: string
  title: string
  url: string
  auth: PluginAuthScheme
  header_name?: string
  cluster?: string | null
  timeout_seconds: number
}

export type PluginPatchInput = Partial<Omit<CustomPluginInput, "id">> & {
  enabled?: boolean
  tools?: Record<string, boolean>
}
