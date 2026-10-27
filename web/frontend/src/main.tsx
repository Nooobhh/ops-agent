import React, { lazy, Suspense } from "react"
import ReactDOM from "react-dom/client"
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom"
import { AppShell } from "@/components/AppShell"
import "./index.css"

const Overview = lazy(() => import("@/pages/Overview"))
const Alerts = lazy(() => import("@/pages/Alerts"))
const AlertDetail = lazy(() => import("@/pages/AlertDetail"))
const NotifySettings = lazy(() => import("@/pages/NotifySettings"))
const ModelSettings = lazy(() => import("@/pages/ModelSettings"))
const AgentSettings = lazy(() => import("@/pages/AgentSettings"))
const AlertConfig = lazy(() => import("@/pages/AlertConfig"))
const Triage = lazy(() => import("@/pages/Triage"))
const TriageDocument = lazy(() => import("@/pages/TriageDocument"))
const Knowledge = lazy(() => import("@/pages/Knowledge"))
const Worklog = lazy(() => import("@/pages/Worklog"))
const WorkItems = lazy(() => import("@/pages/WorkItems"))
const WorkItemDetail = lazy(() => import("@/pages/WorkItemDetail"))
const WorkPlan = lazy(() => import("@/pages/WorkPlan"))
const WeeklyReportSettings = lazy(() => import("@/pages/WeeklyReportSettings"))
const AcquisitionSettings = lazy(() => import("@/pages/AcquisitionSettings"))
const ResourceAlertSettings = lazy(() => import("@/pages/ResourceAlertSettings"))
const ExternalLinksSettings = lazy(() => import("@/pages/ExternalLinksSettings"))
const CredentialsSettings = lazy(() => import("@/pages/CredentialsSettings"))
const PluginSettings = lazy(() => import("@/pages/PluginSettings"))
const LocalUserSettings = lazy(() => import("@/pages/LocalUserSettings"))
const AboutSettings = lazy(() => import("@/pages/AboutSettings"))
const Resources = lazy(() => import("@/pages/Resources"))

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <Suspense fallback={<div className="p-8 text-center text-sm text-muted-foreground">正在加载</div>}>
        <Routes>
          <Route element={<AppShell />}>
            <Route element={<Overview />} index />
            <Route element={<Alerts />} path="alerts" />
            <Route element={<Resources />} path="resources" />
            <Route element={<AlertDetail />} path="alerts/:key" />
            <Route element={<NotifySettings />} path="settings/notify" />
            <Route element={<ModelSettings />} path="settings/models" />
            <Route element={<Navigate replace to="/settings/agent/permissions" />} path="settings/agent" />
            <Route element={<AgentSettings />} path="settings/agent/permissions" />
            <Route element={<AgentSettings />} path="settings/agent/policies" />
            <Route element={<WeeklyReportSettings />} path="settings/weekly-report" />
            <Route element={<AcquisitionSettings />} path="settings/acquisition" />
            <Route element={<AcquisitionSettings />} path="settings/alerts/acquisition" />
            <Route element={<AcquisitionSettings />} path="settings/maintenance" />
            <Route element={<ResourceAlertSettings />} path="settings/resource-alerts" />
            <Route element={<Navigate replace to="/settings/knowledge/vector" />} path="settings/external" />
            <Route element={<Navigate replace to="/settings/knowledge/vector" />} path="settings/knowledge" />
            <Route element={<ExternalLinksSettings />} path="settings/knowledge/vector" />
            <Route element={<ExternalLinksSettings />} path="settings/knowledge/rerank" />
            <Route element={<CredentialsSettings />} path="settings/credentials" />
            <Route element={<PluginSettings />} path="settings/plugins" />
            <Route element={<LocalUserSettings />} path="settings/user" />
            <Route element={<AboutSettings />} path="settings/about" />
            <Route element={<AlertConfig />} path="settings/alerts" />
            <Route element={<TriageDocument />} path="triage/:taskId/report" />
            <Route element={<TriageDocument />} path="reports/troubleshoot/:runId" />
            <Route element={<Triage />} path="triage" />
            <Route element={<Knowledge />} path="knowledge" />
            <Route element={<Knowledge />} path="knowledge/*" />
            <Route element={<WorkItems />} path="work-items" />
            <Route element={<WorkPlan />} path="work-items/plans/:planId" />
            <Route element={<WorkItemDetail />} path="work-items/:itemId" />
            <Route element={<Worklog />} path="worklog" />
          </Route>
        </Routes>
      </Suspense>
    </BrowserRouter>
  </React.StrictMode>,
)
