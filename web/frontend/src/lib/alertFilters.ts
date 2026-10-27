export const ALERT_CLASS_FILTER_OPTIONS = [
  { value: "watch", label: "观察" },
  { value: "chronic", label: "常驻" },
  { value: "ignore", label: "忽略" },
  { value: "flapping", label: "波动" },
] as const

export type AlertClassFilter = (typeof ALERT_CLASS_FILTER_OPTIONS)[number]["value"]

export function normalizeAlertClassFilter(value: string | null): AlertClassFilter[] {
  const requested = new Set((value || "").split(","))
  return ALERT_CLASS_FILTER_OPTIONS
    .map((option) => option.value)
    .filter((item) => requested.has(item))
}

export function alertClassSelectionSummary(values: readonly AlertClassFilter[]): string {
  if (values.length === 0) return "全部"
  const selected = new Set(values)
  return ALERT_CLASS_FILTER_OPTIONS
    .filter((option) => selected.has(option.value))
    .map((option) => option.label)
    .join("、")
}
