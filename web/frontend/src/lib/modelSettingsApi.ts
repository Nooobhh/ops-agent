import { deleteJson, fetchJson, postJson, putJson } from "./api.ts"
import { assertSchemaV1 } from "./triageEvents.ts"
import type {
  AgentTypePolicy,
  ModelSettingsResponse,
  ReasoningEffort,
  TaskType,
  WorkflowModelDefault,
} from "../types.ts"


export const REASONING_LABELS: Record<ReasoningEffort, string> = {
  none: "不指定",
  low: "低",
  medium: "中",
  high: "高",
  xhigh: "极高",
}


export type ModelUpsertInput = {
  title: string
  base_url: string
  api_key?: string
  model: string
  default_reasoning_effort: ReasoningEffort
}


function checked(value: ModelSettingsResponse): ModelSettingsResponse {
  assertSchemaV1(value)
  return value
}


function serializedModel(input: ModelUpsertInput): Omit<ModelUpsertInput, "api_key"> & {
  api_key?: string
} {
  const {api_key, ...fields} = input
  return api_key?.trim() ? {...fields, api_key} : fields
}


export async function loadModelSettings(
  signal?: AbortSignal,
): Promise<ModelSettingsResponse> {
  return checked(await fetchJson<ModelSettingsResponse>(
    "/api/model-settings",
    signal,
  ))
}


export async function createModel(
  input: ModelUpsertInput,
): Promise<ModelSettingsResponse> {
  return checked(await postJson<ModelSettingsResponse>(
    "/api/model-settings/models",
    serializedModel(input),
  ))
}


export async function updateModel(
  modelId: string,
  input: ModelUpsertInput,
): Promise<ModelSettingsResponse> {
  return checked(await putJson<ModelSettingsResponse>(
    `/api/model-settings/models/${encodeURIComponent(modelId)}`,
    serializedModel(input),
  ))
}


export async function deleteModel(
  modelId: string,
): Promise<ModelSettingsResponse> {
  return checked(await deleteJson<ModelSettingsResponse>(
    `/api/model-settings/models/${encodeURIComponent(modelId)}`,
  ))
}


export async function updateDefaults(defaults: {
  auto: WorkflowModelDefault
  manual: WorkflowModelDefault
  weekly: WorkflowModelDefault
  reviewer?: WorkflowModelDefault | null
}): Promise<ModelSettingsResponse> {
  return checked(await putJson<ModelSettingsResponse>(
    "/api/model-settings/defaults",
    defaults,
  ))
}


export async function updateAgentPolicies(
  policies: Record<TaskType, AgentTypePolicy>,
): Promise<ModelSettingsResponse> {
  return checked(await putJson<ModelSettingsResponse>(
    "/api/model-settings/agent-policies",
    policies,
  ))
}
