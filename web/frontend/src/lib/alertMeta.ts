export type AlertMetaInput = {
  chronic: boolean
  class?: string | null
  effective_class: string | null
  muted: boolean
  first_seen: string | null
}

export type AlertMetaToken =
  | {
      kind: "tag"
      status: "chronic" | "flapping" | "muted"
      label: "常驻" | "波动" | "已静音"
    }
  | {
      kind: "duration"
      seconds: number
    }

export function buildAlertMetaTokens(
  item: AlertMetaInput,
  now = Date.now(),
): AlertMetaToken[] {
  const tokens: AlertMetaToken[] = []

  if (item.chronic) {
    tokens.push({ kind: "tag", status: "chronic", label: "常驻" })
  }
  if (item.class === "flapping" || item.effective_class === "flapping") {
    tokens.push({ kind: "tag", status: "flapping", label: "波动" })
  }
  if (item.muted) {
    tokens.push({ kind: "tag", status: "muted", label: "已静音" })
  }

  const firstSeen = item.first_seen ? Date.parse(item.first_seen) : Number.NaN
  if (item.chronic && Number.isFinite(firstSeen)) {
    tokens.push({
      kind: "duration",
      seconds: Math.max(0, (now - firstSeen) / 1000),
    })
  }

  return tokens
}
