export type ReportPeriod = {
  start_date: string
  end_date: string
}

export function filterWeeklyReports<T extends ReportPeriod>(
  reports: T[],
  startDate: string,
  endDate: string,
): T[] {
  if (startDate && endDate && startDate > endDate) return []
  return reports.filter((report) => (
    (!startDate || report.end_date >= startDate)
    && (!endDate || report.start_date <= endDate)
  ))
}
