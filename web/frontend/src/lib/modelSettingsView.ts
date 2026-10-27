import type {
  ModelSettingsResponse,
  WorkflowModelDefault,
} from "../types.ts"


export function manualDefaultSelection(
  settings: ModelSettingsResponse,
): WorkflowModelDefault | null {
  return settings.defaults?.manual || null
}


export function selectionForModel(
  settings: ModelSettingsResponse,
  modelId: string,
): WorkflowModelDefault | null {
  const profile = settings.models.find((item) => item.id === modelId)
  if (!profile) return null
  return {
    model_id: profile.id,
    reasoning_effort: profile.default_reasoning_effort,
    timeout_seconds: settings.defaults?.manual.timeout_seconds ?? 120,
  }
}


export function deleteBlocker(
  settings: ModelSettingsResponse,
  modelId: string,
): string | null {
  if (
    settings.defaults?.auto.model_id === modelId
    || settings.defaults?.manual.model_id === modelId
    || settings.defaults?.weekly.model_id === modelId
    || settings.defaults?.reviewer?.model_id === modelId
  ) {
    return "该模型正在被默认模型配置使用"
  }
  return null
}


export function isSettingsPath(pathname: string): boolean {
  return (
    pathname === "/settings/notify"
    || pathname === "/settings/alerts"
    || pathname === "/settings/models"
    || pathname === "/settings/weekly-report"
    || pathname === "/settings/acquisition"
    || pathname === "/settings/external"
    || pathname === "/settings/credentials"
    || pathname === "/settings/resource-alerts"
    || pathname === "/settings/user"
    || pathname === "/settings/about"
  )
}
