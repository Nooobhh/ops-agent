import { fetchJson, postJson, putJson } from "./api.ts"
import type {
  VectorSettingsInput,
  VectorSettingsResponse,
  VectorSettingsTestResponse,
} from "../types.ts"

export function loadVectorSettings(signal?: AbortSignal) {
  return fetchJson<VectorSettingsResponse>("/api/vector-settings", signal)
}

export function saveVectorSettings(input: VectorSettingsInput) {
  return putJson<VectorSettingsResponse>("/api/vector-settings", input)
}

export function testVectorSettings(input: VectorSettingsInput) {
  return postJson<VectorSettingsTestResponse>("/api/vector-settings/test", input)
}
