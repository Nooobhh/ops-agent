import { fetchJson, putJson } from "./api.ts"
import type { NotifyTemplateSettings } from "../types.ts"

export function loadNotifySettings(signal?: AbortSignal) {
  return fetchJson<NotifyTemplateSettings>("/api/notify/settings", signal)
}

export function saveNotifySettings(template: string) {
  return putJson<NotifyTemplateSettings>("/api/notify/settings", {
    format: "markdown",
    template,
  })
}
