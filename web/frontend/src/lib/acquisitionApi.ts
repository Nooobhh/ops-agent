import { fetchJson, putJson } from "./api.ts"
import type {
  AcquisitionResponse,
  AcquisitionSettings,
  ResourceRisksResponse,
  ResourceHistoryResponse,
  ResourceSummaryResponse,
  ResourcesLatestResponse,
  ResourceAlertSettingsResponse,
} from "../types.ts"

export function loadAcquisition(signal?: AbortSignal) {
  return fetchJson<AcquisitionResponse>("/api/acquisition", signal)
}

export function saveAcquisition(settings: AcquisitionSettings) {
  return putJson<AcquisitionResponse>("/api/acquisition/settings", settings)
}

export function loadResourceAlertSettings(signal?: AbortSignal) {
  return fetchJson<ResourceAlertSettingsResponse>("/api/resource-alert-settings", signal)
}

export function saveResourceAlertSettings(overrides: Record<string, unknown>) {
  return putJson<ResourceAlertSettingsResponse>("/api/resource-alert-settings", {
    schema_version: 1,
    overrides,
  })
}

export function loadLatestResources(signal?: AbortSignal) {
  return fetchJson<ResourcesLatestResponse>("/api/resources/latest", signal)
}

export function loadResourceRisks(signal?: AbortSignal) {
  return fetchJson<ResourceRisksResponse>("/api/resource-risks", signal)
}

export function loadResourceSummary(signal?: AbortSignal) {
  return fetchJson<ResourceSummaryResponse>("/api/resources/summary", signal)
}

export function loadResourceHistory(
  platform: string, node: string, hours: number, signal?: AbortSignal,
) {
  const query = new URLSearchParams({ platform, node, hours: String(hours) })
  return fetchJson<ResourceHistoryResponse>(`/api/resources/history?${query}`, signal)
}
