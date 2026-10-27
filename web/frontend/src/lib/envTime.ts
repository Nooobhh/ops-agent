// 环境时区:由 AppShell 启动时从 /api/env 取得后设置;业务时间一律按它展示与分桶,不看浏览器时区。
// 取不到时(接口失败)才退回浏览器时区。
let envTimeZone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"

export function setEnvTimeZone(value?: string | null): void {
  if (value) envTimeZone = value
}

export function getEnvTimeZone(): string {
  return envTimeZone
}

/** 某一时刻在环境时区的日期 YYYY-MM-DD。 */
export function zonedDate(at: Date | number = new Date(), timeZone = envTimeZone): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone, year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(new Date(at))
  const value = Object.fromEntries(parts.map((part) => [part.type, part.value]))
  return `${value.year}-${value.month}-${value.day}`
}

/** 环境时区相对 UTC 的偏移(毫秒,东区为正),按给定时刻取以兼容夏令时。 */
export function zoneOffsetMs(at: number, timeZone = envTimeZone): number {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).formatToParts(new Date(at))
  const value = Object.fromEntries(parts.map((part) => [part.type, Number(part.value)]))
  const wall = Date.UTC(value.year, value.month - 1, value.day, value.hour, value.minute, value.second)
  return wall - Math.floor(at / 1000) * 1000
}

/** 环境时区某日 00:00 对应的时刻(ISO)。 */
export function zonedMidnightIso(day: string, timeZone = envTimeZone): string {
  const guess = Date.parse(`${day}T00:00:00Z`)
  return new Date(guess - zoneOffsetMs(guess - zoneOffsetMs(guess, timeZone), timeZone)).toISOString()
}
