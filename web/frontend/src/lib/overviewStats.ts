export const OVERVIEW_STAT_CARDS = [
  {
    key: "active_non_chronic",
    label: "活跃（非常驻）",
    filterUrl: "/alerts?chronic=exclude&status=active,suppressed",
  },
  {
    key: "new_today",
    label: "今日新增",
    filterUrl: "today",
  },
  {
    key: "suppressed_today",
    label: "今日抑制",
    filterUrl: "/alerts?suppressed_today=true",
  },
  {
    key: "chronic",
    label: "常驻",
    filterUrl: "/alerts?chronic=only",
  },
] as const

export type OverviewStatKey = typeof OVERVIEW_STAT_CARDS[number]["key"]

export function overviewStatTone(key: OverviewStatKey, value: number) {
  if (key === "active_non_chronic") return "text-red-600 dark:text-red-400"
  if (key === "chronic") return "text-amber-600 dark:text-amber-400"
  if (key === "new_today") {
    if (value < 10) return "text-emerald-600 dark:text-emerald-400"
    if (value <= 20) return "text-amber-600 dark:text-amber-400"
    return "text-red-600 dark:text-red-400"
  }
  return "text-foreground"
}
