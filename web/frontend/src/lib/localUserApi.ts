import { fetchJson, putJson } from "./api"
import type { LocalUserResponse } from "@/types"

export function loadLocalUser(signal?: AbortSignal): Promise<LocalUserResponse> {
  return fetchJson<LocalUserResponse>("/api/local-user", signal)
}

export function saveLocalUser(id: string, displayName: string): Promise<LocalUserResponse> {
  return putJson<LocalUserResponse>("/api/local-user", { id, display_name: displayName })
}

/** 与后端 lib.datastore.models.AUTHOR_ID_RE 一致:小写字母数字开头,可含 _ . -,最长 64。 */
export function isValidUserId(value: string): boolean {
  return /^[a-z0-9][a-z0-9_.-]{0,63}$/.test(value)
}
