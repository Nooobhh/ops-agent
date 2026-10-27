import { fetchJson, patchJson, postJson, putJson } from "./api.ts"
import { assertSchemaV1 } from "./triageEvents.ts"
import type {
  ApprovalDecision,
  ApprovalScope,
  CommandMode,
  CommandPolicyResponse,
  CreateTriageTaskRequest,
  TriageHealth,
  TriageTaskDetail,
  TriageTaskEventsResponse,
  TriageTaskListResponse,
  RuntimeControlUpdates,
  ResourceRiskTaskRequest,
  TaskArtifactListResponse,
  ToolCatalogResponse,
} from "../types.ts"


function taskPath(taskId: string): string {
  return `/api/triage-tasks/${encodeURIComponent(taskId)}`
}


function checked<T extends {schema_version?: number}>(value: T): T {
  assertSchemaV1(value)
  return value
}


export async function loadTriageHealth(
  signal?: AbortSignal,
): Promise<TriageHealth> {
  return checked(await fetchJson<TriageHealth>("/api/triage-health", signal))
}


export async function loadTriageToolCatalog(
  options: {agentId?: string; phase?: string; source?: string} = {},
  signal?: AbortSignal,
): Promise<ToolCatalogResponse> {
  const params = new URLSearchParams()
  if (options.agentId) params.set("agent_id", options.agentId)
  if (options.phase) params.set("phase", options.phase)
  if (options.source) params.set("source", options.source)
  const query = params.size ? `?${params}` : ""
  return checked(await fetchJson<ToolCatalogResponse>(
    `/api/triage-tools${query}`,
    signal,
  ))
}


export async function listTriageTasks(
  signal?: AbortSignal,
): Promise<TriageTaskListResponse> {
  return checked(await fetchJson<TriageTaskListResponse>(
    "/api/triage-tasks?limit=200",
    signal,
  ))
}


export async function loadTriageTask(
  taskId: string,
  signal?: AbortSignal,
): Promise<TriageTaskDetail> {
  return checked(await fetchJson<TriageTaskDetail>(taskPath(taskId), signal))
}


export async function loadTriageEvents(
  taskId: string,
  after?: string,
  signal?: AbortSignal,
): Promise<TriageTaskEventsResponse> {
  const params = new URLSearchParams()
  if (after) params.set("after", after)
  const query = params.size ? `?${params}` : ""
  return checked(await fetchJson<TriageTaskEventsResponse>(
    `${taskPath(taskId)}/events${query}`,
    signal,
  ))
}


export async function createTriageTask(
  request: CreateTriageTaskRequest,
  workItemId?: string,
): Promise<TriageTaskDetail> {
  const query = workItemId ? `?${new URLSearchParams({work_item_id: workItemId})}` : ""
  return checked(await postJson<TriageTaskDetail>(
    `/api/triage-tasks${query}`,
    request,
  ))
}


export async function createTriageTaskFromResourceRisk(
  riskRef: string,
  request: ResourceRiskTaskRequest = {},
): Promise<TriageTaskDetail> {
  return checked(await postJson<TriageTaskDetail>(
    `/api/resource-risks/${encodeURIComponent(riskRef)}/triage-task`,
    request,
  ))
}


export async function addTriageMessage(
  taskId: string,
  message: string,
): Promise<TriageTaskDetail> {
  return checked(await postJson<TriageTaskDetail>(
    `${taskPath(taskId)}/messages`,
    {message},
  ))
}


export async function retryTriageTask(
  taskId: string,
): Promise<TriageTaskDetail> {
  return checked(await postJson<TriageTaskDetail>(
    `${taskPath(taskId)}/retry`,
    {},
  ))
}


export async function updateTriageRuntimeControls(
  taskId: string,
  updates: RuntimeControlUpdates,
): Promise<TriageTaskDetail> {
  const response = await fetch(`${taskPath(taskId)}/runtime-controls`, {
    method: "PATCH",
    headers: {Accept: "application/json", "Content-Type": "application/json"},
    body: JSON.stringify(updates),
  })
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as {detail?: string} | null
    throw new Error(payload?.detail || `请求失败 (${response.status})`)
  }
  return checked(await response.json() as TriageTaskDetail)
}


export async function loadCommandPolicy(
  signal?: AbortSignal,
): Promise<CommandPolicyResponse> {
  return fetchJson<CommandPolicyResponse>("/api/command-policy", signal)
}


export async function updateCommandPolicy(
  mode: CommandMode,
): Promise<CommandPolicyResponse> {
  return putJson<CommandPolicyResponse>("/api/command-policy", {mode})
}


export async function decideTriageApproval(
  taskId: string,
  approvalId: string,
  decision: ApprovalDecision,
  feedback?: string,
  scope?: ApprovalScope,
): Promise<TriageTaskDetail> {
  const body: {decision: ApprovalDecision; feedback?: string; scope?: ApprovalScope} = {decision}
  if (feedback) body.feedback = feedback
  if (scope) body.scope = scope
  return checked(await postJson<TriageTaskDetail>(
    `${taskPath(taskId)}/approvals/${encodeURIComponent(approvalId)}`,
    body,
  ))
}


async function fetchMarkdown(
  url: string,
  signal?: AbortSignal,
): Promise<string> {
  const response = await fetch(url, {signal})
  if (!response.ok) {
    throw new Error(
      response.status === 404 ? "文档尚未生成" : `读取失败 (${response.status})`,
    )
  }
  return response.text()
}


export async function loadTriageReport(
  taskId: string,
  signal?: AbortSignal,
): Promise<string> {
  return fetchMarkdown(`${taskPath(taskId)}/report`, signal)
}

export async function loadTroubleshootReport(
  runId: string,
  signal?: AbortSignal,
): Promise<string> {
  return fetchMarkdown(
    `/api/troubleshoot-reports/${encodeURIComponent(runId)}`,
    signal,
  )
}


export async function loadTriageKbDoc(
  taskId: string,
  signal?: AbortSignal,
): Promise<string> {
  return fetchMarkdown(`${taskPath(taskId)}/kb-doc`, signal)
}


export async function listTaskArtifacts(
  taskId: string,
  signal?: AbortSignal,
): Promise<TaskArtifactListResponse> {
  return checked(await fetchJson<TaskArtifactListResponse>(
    `${taskPath(taskId)}/artifacts`,
    signal,
  ))
}


export async function loadTaskArtifact(
  taskId: string,
  filename: string,
  signal?: AbortSignal,
): Promise<string> {
  return fetchMarkdown(
    `${taskPath(taskId)}/artifacts/${encodeURIComponent(filename)}`,
    signal,
  )
}


export function taskArtifactDownloadUrl(taskId: string, filename: string): string {
  return `${taskPath(taskId)}/artifacts/${encodeURIComponent(filename)}?download=true`
}


export async function cancelTriageTask(
  taskId: string,
): Promise<TriageTaskDetail> {
  return checked(await postJson<TriageTaskDetail>(
    `${taskPath(taskId)}/cancel`,
    {},
  ))
}


export async function completeTriageTask(
  taskId: string,
): Promise<TriageTaskDetail> {
  return checked(await postJson<TriageTaskDetail>(
    `${taskPath(taskId)}/complete`,
    {},
  ))
}


export async function setTriageTaskArchived(
  taskId: string,
  archived: boolean,
): Promise<TriageTaskDetail> {
  return checked(await postJson<TriageTaskDetail>(
    `${taskPath(taskId)}/${archived ? "archive" : "unarchive"}`,
    {},
  ))
}


export async function renameTriageTask(
  taskId: string,
  title: string,
): Promise<TriageTaskDetail> {
  return checked(await patchJson<TriageTaskDetail>(
    taskPath(taskId),
    {title},
  ))
}
