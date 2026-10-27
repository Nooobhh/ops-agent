import { deleteJson, fetchJson, postJson, putJson } from "./api"
import type {
  WeeklyReportCreateResponse,
  WeeklyReportDocumentPreview,
  WeeklyReportListResponse,
  WeeklyReportJob,
  WeeklyReportJobListResponse,
  WeeklyReportSettings,
  WorklogCreateInput,
  WorklogCreateResponse,
  WorklogDetailResponse,
  WorklogResponse,
  WorklogWeeklyReportExclusionResponse,
  WeeklyReportDeleteResponse,
} from "@/types"

export function loadWeeklyReportSettings(signal?: AbortSignal): Promise<WeeklyReportSettings> {
  return fetchJson<WeeklyReportSettings>("/api/weekly-report-settings", signal)
}

export function updateWeeklyReportSettings(
  startWeekday: number,
  endWeekday: number,
): Promise<WeeklyReportSettings> {
  return putJson<WeeklyReportSettings>("/api/weekly-report-settings", {
    start_weekday: startWeekday,
    end_weekday: endWeekday,
  })
}

export function loadWorklogs(
  startDate: string,
  endDate: string,
  signal?: AbortSignal,
): Promise<WorklogResponse> {
  const params = new URLSearchParams({start_date: startDate, end_date: endDate})
  return fetchJson<WorklogResponse>(`/api/worklogs?${params}`, signal)
}

export function createWorklog(input: WorklogCreateInput): Promise<WorklogCreateResponse> {
  return postJson<WorklogCreateResponse>("/api/worklogs", input)
}

export function loadWorklogDetail(
  date: string,
  entryIndex: number,
  signal?: AbortSignal,
): Promise<WorklogDetailResponse> {
  return fetchJson<WorklogDetailResponse>(
    `/api/worklogs/${encodeURIComponent(date)}/${entryIndex}`,
    signal,
  )
}

export function setWorklogWeeklyReportExcluded(
  date: string,
  entryIndex: number,
  excluded: boolean,
): Promise<WorklogWeeklyReportExclusionResponse> {
  return putJson<WorklogWeeklyReportExclusionResponse>(
    `/api/worklogs/${encodeURIComponent(date)}/${entryIndex}/weekly-report-exclusion`,
    { excluded },
  )
}

export function listWeeklyReports(signal?: AbortSignal): Promise<WeeklyReportListResponse> {
  return fetchJson<WeeklyReportListResponse>("/api/weekly-reports", signal)
}

export function listWeeklyReportJobs(
  startDate?: string,
  endDate?: string,
  signal?: AbortSignal,
): Promise<WeeklyReportJobListResponse> {
  const params = new URLSearchParams()
  if (startDate) params.set("start_date", startDate)
  if (endDate) params.set("end_date", endDate)
  const query = params.toString()
  return fetchJson<WeeklyReportJobListResponse>(
    `/api/weekly-report-jobs${query ? `?${query}` : ""}`,
    signal,
  )
}

export function getWeeklyReportJob(jobId: string): Promise<WeeklyReportJob> {
  return fetchJson<WeeklyReportJob>(`/api/weekly-report-jobs/${encodeURIComponent(jobId)}`)
}

export function generateWeeklyReport(
  startDate: string,
  endDate: string,
): Promise<WeeklyReportCreateResponse> {
  return postJson<WeeklyReportCreateResponse>("/api/weekly-reports", {
    start_date: startDate,
    end_date: endDate,
  })
}

export function regenerateWeeklyReport(filename: string): Promise<WeeklyReportCreateResponse> {
  return putJson<WeeklyReportCreateResponse>(weeklyReportDownloadUrl(filename), {})
}

export function deleteWeeklyReport(filename: string): Promise<WeeklyReportDeleteResponse> {
  return deleteJson<WeeklyReportDeleteResponse>(weeklyReportDownloadUrl(filename))
}

export function weeklyReportDownloadUrl(filename: string): string {
  return `/api/weekly-reports/${encodeURIComponent(filename)}`
}

export function revealWeeklyReport(filename: string): Promise<{ filename: string; folder: string }> {
  return postJson<{ filename: string; folder: string }>(
    `/api/weekly-reports/${encodeURIComponent(filename)}/reveal`,
    {},
  )
}

export function weeklyReportPreviewUrl(filename: string): string {
  return `/api/weekly-reports/${encodeURIComponent(filename)}/preview`
}

export function loadWeeklyReportPreview(
  filename: string,
  signal?: AbortSignal,
): Promise<WeeklyReportDocumentPreview> {
  return fetchJson<WeeklyReportDocumentPreview>(weeklyReportPreviewUrl(filename), signal)
}
