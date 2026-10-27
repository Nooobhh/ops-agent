import type {
  CustomPluginInput,
  PluginConnectionInput,
  PluginCredentialInput,
  PluginHealthResponse,
  PluginHealthState,
  PluginPatchInput,
  PluginsResponse,
  PluginTestResponse,
  PluginView,
} from "../types.ts"
import { deleteJson, fetchJson, patchJson, postJson, putJson } from "./api.ts"

export function loadPlugins(signal?: AbortSignal) {
  return fetchJson<PluginsResponse>("/api/plugins", signal)
}

export function loadPluginHealth(signal?: AbortSignal) {
  return fetchJson<PluginHealthResponse>("/api/plugins/health", signal)
}

export function createPlugin(input: CustomPluginInput) {
  return postJson<PluginView>("/api/plugins", input)
}

export function patchPlugin(id: string, input: PluginPatchInput) {
  return patchJson<PluginView>(`/api/plugins/${encodeURIComponent(id)}`, input)
}

export function deletePlugin(id: string) {
  return deleteJson<{ deleted: string }>(`/api/plugins/${encodeURIComponent(id)}`)
}

export function resetPlugin(id: string) {
  return postJson<PluginView>(`/api/plugins/${encodeURIComponent(id)}/reset`, {})
}

export function savePluginCredential(id: string, input: PluginCredentialInput) {
  return putJson<PluginView>(`/api/plugins/${encodeURIComponent(id)}/credential`, input)
}

/** 连接列表认证:整表替换;账号 / 密码留空沿用同名连接原值,只写不回显。 */
export function savePluginConnections(id: string, connections: PluginConnectionInput[]) {
  return putJson<PluginView>(`/api/plugins/${encodeURIComponent(id)}/connections`, { connections })
}

/** 写操作审批密钥:与插件服务目录的 approval.secret 同一串;只写不回显。 */
export function savePluginApprovalSecret(id: string, secret: string) {
  return putJson<PluginView>(`/api/plugins/${encodeURIComponent(id)}/approval-secret`, { secret })
}

export function testPlugin(id: string) {
  return postJson<PluginTestResponse>(`/api/plugins/${encodeURIComponent(id)}/test`, {})
}

export function testDraftPlugin(input: Omit<CustomPluginInput, "id" | "title" | "cluster"> & { credential?: PluginCredentialInput }) {
  return postJson<PluginTestResponse>("/api/plugins/test", input)
}

export const HEALTH_LABELS: Record<PluginHealthState, string> = {
  ok: "在线",
  unreachable: "连不上",
  auth_failed: "认证失败",
  protocol_error: "协议异常",
  not_configured: "缺凭据",
}

/** 顶栏「插件 健康/总数」只数已启用的插件;没有启用的插件时不显示。 */
export function enabledPluginHealth(response: PluginHealthResponse | null): { healthy: number; total: number } {
  const enabled = Object.values(response?.plugins || {}).filter((item) => item.enabled)
  return { healthy: enabled.filter((item) => item.state === "ok").length, total: enabled.length }
}

/** 各认证方式要填的凭据字段;none 不需要凭据。 */
export const CREDENTIAL_FIELDS: Record<string, Array<{ key: keyof PluginCredentialInput; label: string; secret: boolean }>> = {
  none: [],
  basic: [{ key: "user", label: "账号", secret: false }, { key: "password", label: "密码", secret: true }],
  bearer: [{ key: "token", label: "Token", secret: true }],
  header: [{ key: "value", label: "请求头的值", secret: true }],
  connections: [],  // 连接列表另有编辑区
}
