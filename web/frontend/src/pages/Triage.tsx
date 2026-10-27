import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { AlertTriangle, Info, List, PanelLeftOpen, PanelRight, PanelRightOpen } from "lucide-react"
import { Link, useLocation, useNavigate, useSearchParams } from "react-router-dom"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { Conversation } from "@/components/triage/Conversation"
import { TaskContext } from "@/components/triage/TaskContext"
import { TaskList } from "@/components/triage/TaskList"
import {
  addTriageMessage,
  cancelTriageTask,
  completeTriageTask,
  createTriageTask,
  decideTriageApproval,
  listTriageTasks,
  loadTriageEvents,
  loadTriageHealth,
  loadTriageTask,
  renameTriageTask,
  retryTriageTask,
  setTriageTaskArchived,
  loadCommandPolicy,
  updateTriageRuntimeControls,
} from "@/lib/triageApi"
import { loadModelSettings } from "@/lib/modelSettingsApi"
import { manualDefaultSelection } from "@/lib/modelSettingsView"
import { isTerminalTask, mergeTaskEvents } from "@/lib/triageEvents"
import {
  canCreateTask,
  pollDelay,
  shouldPromptForMissingModel,
} from "@/lib/triageView"
import { draftFromNavigationState } from "@/lib/triageNavigation"
import type {
  ApprovalDecision,
  CreateTriageTaskRequest,
  ModelSettingsResponse,
  TriageEvent,
  TriageHealth,
  TriageTask,
  TriageTaskDetail,
  CommandMode,
  RuntimeControlUpdates,
} from "@/types"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"


function upsertTask(tasks: TriageTask[], task: TriageTask): TriageTask[] {
  const next = tasks.some((item) => item.task_id === task.task_id)
    ? tasks.map((item) => item.task_id === task.task_id ? task : item)
    : [task, ...tasks]
  return [...next].sort((left, right) => right.created_at.localeCompare(left.created_at))
}


function createUnavailableReason(
  health: TriageHealth | null,
  settings: ModelSettingsResponse | null,
): string {
  if (!health) return "Agent-Ops 服务状态不可用"
  if (health.model === "not_configured") return "本地模型尚未配置"
  if (health.worker.state !== "running") return "Agent-Ops worker 当前未运行"
  if (!settings?.defaults?.manual) return "尚未设置人工任务默认模型"
  return "暂时无法创建任务"
}

type TaskViewState = {
  pinned?: boolean
  unread?: boolean
}

type TaskViewStateMap = Record<string, TaskViewState>

const TRIAGE_LAYOUT_STORAGE_KEY = "ops-triage-layout:v1"
const TRIAGE_TASK_VIEW_STORAGE_KEY = "ops-triage-task-view:v1"

function readLayoutState() {
  const fallback = {taskCollapsed: false, contextCollapsed: false}
  try {
    const raw = localStorage.getItem(TRIAGE_LAYOUT_STORAGE_KEY)
    const value = raw ? JSON.parse(raw) as Partial<typeof fallback> : null
    return {
      taskCollapsed: value?.taskCollapsed === true,
      contextCollapsed: value?.contextCollapsed === true,
    }
  } catch {
    return fallback
  }
}

function readTaskViewState(): TaskViewStateMap {
  try {
    const raw = localStorage.getItem(TRIAGE_TASK_VIEW_STORAGE_KEY)
    const value = raw ? JSON.parse(raw) as Record<string, Partial<TaskViewState>> : null
    if (!value || typeof value !== "object") return {}
    return Object.fromEntries(Object.entries(value).flatMap(([taskId, state]) => {
      if (!state || typeof state !== "object") return []
      const next = {
        ...(state.pinned === true ? {pinned: true} : {}),
        ...(state.unread === true ? {unread: true} : {}),
      }
      return Object.keys(next).length ? [[taskId, next]] : []
    }))
  } catch {
    return {}
  }
}


export default function Triage() {
  const location = useLocation()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const selectedTaskId = searchParams.get("task")
  const [tasks, setTasks] = useState<TriageTask[]>([])
  const [health, setHealth] = useState<TriageHealth | null>(null)
  const [modelSettings, setModelSettings] = useState<ModelSettingsResponse | null>(null)
  const [modelSettingsError, setModelSettingsError] = useState("")
  const [detail, setDetail] = useState<TriageTaskDetail | null>(null)
  const [events, setEvents] = useState<TriageEvent[]>([])
  const [indexLoading, setIndexLoading] = useState(true)
  const [detailLoading, setDetailLoading] = useState(false)
  const [indexError, setIndexError] = useState("")
  const [connectionIssue, setConnectionIssue] = useState("")
  const [mutation, setMutation] = useState<string | null>(null)
  const [pendingMessage, setPendingMessage] = useState<{taskId: string | null; text: string} | null>(null)
  const [taskDraft, setTaskDraft] = useState<ReturnType<typeof draftFromNavigationState>>(null)
  const [missingModelOpen, setMissingModelOpen] = useState(false)
  const [commandMode, setCommandMode] = useState<CommandMode>("whitelist")
  const [taskSheetOpen, setTaskSheetOpen] = useState(false)
  const [contextSheetOpen, setContextSheetOpen] = useState(false)
  const [layoutState, setLayoutState] = useState(readLayoutState)
  const [taskViewState, setTaskViewState] = useState<TaskViewStateMap>(readTaskViewState)
  const eventsRef = useRef<TriageEvent[]>([])
  const mutationRef = useRef(false)

  eventsRef.current = events

  useEffect(() => {
    try { localStorage.setItem(TRIAGE_LAYOUT_STORAGE_KEY, JSON.stringify(layoutState)) } catch { /* no-op */ }
  }, [layoutState])

  useEffect(() => {
    try { localStorage.setItem(TRIAGE_TASK_VIEW_STORAGE_KEY, JSON.stringify(taskViewState)) } catch { /* no-op */ }
  }, [taskViewState])

  // draft 必须在 effect 内解析:放在顶层会每次 render 产生新对象,
  // 作为依赖配合 effect 内的 navigate 会自激成无限 replace 循环(弹窗关不掉)
  useEffect(() => {
    const draft = draftFromNavigationState(location.state)
    if (!draft) return
    setTaskDraft(draft)
    setSearchParams({})
    void navigate(`${location.pathname}${location.search}`, {
      replace: true,
      state: null,
    })
  }, [location.pathname, location.search, location.state, navigate])

  const updateTaskViewState = useCallback((taskId: string, updates: TaskViewState) => {
    setTaskViewState((current) => {
      const next = {...current[taskId], ...updates}
      if (!next.pinned && !next.unread) {
        const {[taskId]: _removed, ...rest} = current
        return rest
      }
      return {...current, [taskId]: next}
    })
  }, [])

  const selectTask = useCallback((taskId: string) => {
    updateTaskViewState(taskId, {unread: false})
    setSearchParams({task: taskId})
    setTaskSheetOpen(false)
  }, [setSearchParams, updateTaskViewState])

  const pinnedTaskIds = useMemo(
    () => new Set(Object.entries(taskViewState).filter(([, state]) => state.pinned).map(([taskId]) => taskId)),
    [taskViewState],
  )
  const unreadTaskIds = useMemo(
    () => new Set(Object.entries(taskViewState).filter(([, state]) => state.unread).map(([taskId]) => taskId)),
    [taskViewState],
  )

  const refreshIndex = useCallback(async (signal?: AbortSignal) => {
      const [healthResponse, taskResponse, settingsResult, policyResult] = await Promise.all([
      loadTriageHealth(signal),
      listTriageTasks(signal),
        loadModelSettings(signal).then(
        (value) => ({value, error: ""}),
        (reason: Error) => ({value: null, error: reason.message}),
        ),
        loadCommandPolicy(signal).catch(() => null),
    ])
    setHealth(healthResponse)
    setTasks(taskResponse.items)
    if (settingsResult.value) {
      setModelSettings(settingsResult.value)
      setModelSettingsError("")
    } else if ((settingsResult.error as string) !== "This operation was aborted") {
      setModelSettingsError(settingsResult.error)
    }
    if (policyResult) setCommandMode(policyResult.mode)
    setIndexError("")
    return taskResponse.items
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    setIndexLoading(true)
    refreshIndex(controller.signal)
      .then((items) => {
        void items
      })
      .catch((reason) => {
        if ((reason as Error).name !== "AbortError") {
          setIndexError((reason as Error).message)
        }
      })
      .finally(() => setIndexLoading(false))
    return () => controller.abort()
  }, [refreshIndex, selectTask, selectedTaskId])

  useEffect(() => {
    const controller = new AbortController()
    const timer = window.setInterval(() => {
      void refreshIndex(controller.signal).catch((reason) => {
        if ((reason as Error).name !== "AbortError") {
          setIndexError((reason as Error).message)
        }
      })
    }, 15_000)
    return () => {
      controller.abort()
      window.clearInterval(timer)
    }
  }, [refreshIndex])

  const applyDetail = useCallback((response: TriageTaskDetail) => {
    setDetail(response)
    setEvents(response.events)
    setTasks((current) => upsertTask(current, response.task))
  }, [])

  const refreshSelected = useCallback(async (signal?: AbortSignal) => {
    if (!selectedTaskId) {
      setDetail(null)
      setEvents([])
      return
    }
    const response = await loadTriageTask(selectedTaskId, signal)
    applyDetail(response)
    setConnectionIssue("")
  }, [applyDetail, selectedTaskId])

  useEffect(() => {
    const controller = new AbortController()
    setDetailLoading(true)
    setDetail(null)
    setEvents([])
    refreshSelected(controller.signal)
      .catch((reason) => {
        if ((reason as Error).name !== "AbortError") {
          setConnectionIssue((reason as Error).message)
        }
      })
      .finally(() => setDetailLoading(false))
    return () => controller.abort()
  }, [refreshSelected])

  useEffect(() => {
    if (!selectedTaskId || !detail || isTerminalTask(detail.task.status)) return
    let stopped = false
    let failureCount = 0
    let timer = 0
    let controller: AbortController | null = null

    const schedule = (delay: number) => {
      timer = window.setTimeout(() => { void tick() }, delay)
    }
    const tick = async () => {
      controller = new AbortController()
      const lastEventId = eventsRef.current.at(-1)?.event_id
      try {
        const [eventResponse, taskResponse] = await Promise.all([
          loadTriageEvents(selectedTaskId, lastEventId, controller.signal),
          loadTriageTask(selectedTaskId, controller.signal),
        ])
        if (stopped) return
        setEvents((current) => mergeTaskEvents(current, eventResponse.items))
        setDetail(taskResponse)
        setTasks((current) => upsertTask(current, taskResponse.task))
        setConnectionIssue("")
        failureCount = 0
        if (!isTerminalTask(taskResponse.task.status)) schedule(1500)
      } catch (reason) {
        if (stopped || (reason as Error).name === "AbortError") return
        failureCount += 1
        setConnectionIssue((reason as Error).message)
        schedule(pollDelay(failureCount))
      }
    }
    schedule(1500)
    return () => {
      stopped = true
      controller?.abort()
      window.clearTimeout(timer)
    }
  }, [detail?.task.status, selectedTaskId])

  async function runMutation<T>(
    key: string,
    action: () => Promise<TriageTaskDetail>,
  ): Promise<TriageTaskDetail> {
    if (mutationRef.current) throw new Error("另一个操作正在提交")
    try {
      mutationRef.current = true
      setMutation(key)
      const response = await action()
      applyDetail(response)
      return response
    } finally {
      mutationRef.current = false
      setMutation(null)
    }
  }

  async function handleCreate(request: CreateTriageTaskRequest) {
    if (!createEnabled) throw new Error(createReason)
    setPendingMessage({taskId: null, text: request.message})
    try {
      const response = await runMutation("create", () => createTriageTask(request, taskDraft?.work_item?.id))
      setTaskDraft(null)
      selectTask(response.task.task_id)
    } finally {
      setPendingMessage(null)
    }
  }

  async function handleSend(message: string) {
    if (!selectedTaskId) return
    setPendingMessage({taskId: selectedTaskId, text: message})
    try {
      await runMutation("message", () => addTriageMessage(selectedTaskId, message))
    } finally {
      setPendingMessage(null)
    }
  }

  async function handleGenerateKb() {
    await handleSend("请基于当前执行报告生成知识库草稿，等待我确认后再落档。")
  }

  async function handleGenerateWorklog() {
    await handleSend("请生成或更新本次任务的 worklog。")
  }

  async function handleApproval(
    approvalId: string,
    decision: ApprovalDecision,
    feedback?: string,
  ) {
    if (!selectedTaskId) return
    await runMutation("approval", () => decideTriageApproval(
      selectedTaskId,
      approvalId,
      decision,
      feedback,
    ))
  }

  async function handleRuntimeControls(taskId: string, updates: RuntimeControlUpdates) {
    const response = await runMutation("runtime-controls", () => updateTriageRuntimeControls(taskId, updates))
    return response
  }

  async function handleCancel() {
    if (!selectedTaskId) return
    await runMutation("cancel", () => cancelTriageTask(selectedTaskId))
  }

  async function handleComplete() {
    if (!selectedTaskId) return
    await runMutation("complete", () => completeTriageTask(selectedTaskId))
  }

  async function handleRetry() {
    if (!selectedTaskId) return
    await runMutation("retry", () => retryTriageTask(selectedTaskId))
  }

  async function handleArchive(taskId: string, archived: boolean) {
    if (mutationRef.current) throw new Error("另一个操作正在提交")
    try {
      mutationRef.current = true
      setMutation("archive")
      const response = await setTriageTaskArchived(taskId, archived)
      setTasks((current) => upsertTask(current, response.task))
      if (taskId === selectedTaskId) {
        setDetail(response)
        setEvents(response.events)
        // 归档当前选中任务后清掉选中,交回自动选中逻辑:有可见任务选最新一条,否则空态
        if (archived) setSearchParams({})
      }
    } finally {
      mutationRef.current = false
      setMutation(null)
    }
  }

  async function handleRename(taskId: string, title: string) {
    if (mutationRef.current) throw new Error("另一个操作正在提交")
    try {
      mutationRef.current = true
      setMutation("rename")
      const response = await renameTriageTask(taskId, title)
      setTasks((current) => upsertTask(current, response.task))
      if (taskId === selectedTaskId) {
        setDetail(response)
        setEvents(response.events)
      }
    } finally {
      mutationRef.current = false
      setMutation(null)
    }
  }

  const selectedTask = detail?.task ?? tasks.find((task) => task.task_id === selectedTaskId) ?? null
  const manualDefault = modelSettings ? manualDefaultSelection(modelSettings) : null
  const missingModel = shouldPromptForMissingModel(health, manualDefault)
  const createEnabled = canCreateTask(health) && manualDefault !== null
  // 缺模型时仍允许进入新建态并弹说明；真正发送继续受 createEnabled 约束。
  const newTaskEntryEnabled = createEnabled || missingModel
  const createReason = createUnavailableReason(health, modelSettings)
  const workerUnavailable = health?.model === "configured" && health.worker.state !== "running"
  const sideButtons = (
    <div className="triage-mobile-actions">
      <Button
        aria-label="打开任务列表"
        onClick={() => setTaskSheetOpen(true)}
        size="icon"
        title="任务列表"
        variant="outline"
      >
        <List />
      </Button>
      <Button
        aria-label="打开任务上下文"
        disabled={!selectedTask}
        onClick={() => setContextSheetOpen(true)}
        size="icon"
        title="任务上下文"
        variant="outline"
      >
        <PanelRight />
      </Button>
    </div>
  )

  return (
    <div className="triage-page">
      {health?.model === "not_configured" ? (
        <Alert className="triage-health-banner rounded-md">
          <Info />
          <AlertTitle>本地模型未配置</AlertTitle>
          <AlertDescription>
            历史任务仍可浏览。<Link className="font-medium underline underline-offset-4" to="/settings/models">前往模型配置</Link>
          </AlertDescription>
        </Alert>
      ) : workerUnavailable ? (
        <Alert className="triage-health-banner rounded-md border-[#d69e2e]/50 bg-[#d69e2e]/10">
          <AlertTriangle />
          <AlertTitle>Agent-Ops worker 未运行</AlertTitle>
          <AlertDescription>未完成任务会等待 worker 恢复，页面不会把它们显示为正在执行。</AlertDescription>
        </Alert>
      ) : null}
      {indexError ? (
        <Alert className="triage-health-banner rounded-md" variant="destructive">
          <AlertTriangle />
          <AlertDescription>{indexError}。已保留现有任务，后台会继续重试。</AlertDescription>
        </Alert>
      ) : null}
      {modelSettingsError && modelSettings ? (
        <Alert className="triage-health-banner rounded-md border-[#d69e2e]/50 bg-[#d69e2e]/10">
          <AlertTriangle />
          <AlertDescription>
            模型设置刷新失败，当前继续使用上一次加载的选项。
          </AlertDescription>
        </Alert>
      ) : null}

      <div
        className="triage-layout"
        data-context-collapsed={layoutState.contextCollapsed}
        data-task-collapsed={layoutState.taskCollapsed}
      >
        <aside className="triage-task-pane">
          {layoutState.taskCollapsed ? (
            <div className="triage-collapsed-rail">
              <Button aria-label="展开任务列表" onClick={() => setLayoutState((current) => ({...current, taskCollapsed: false}))} size="icon" title="展开任务列表" variant="ghost">
                <PanelLeftOpen />
              </Button>
            </div>
          ) : (
            <TaskList
              createEnabled={newTaskEntryEnabled}
              createReason={createReason}
              loading={indexLoading}
              onArchive={(taskId, archived) => void handleArchive(taskId, archived)}
              onPin={(taskId, pinned) => updateTaskViewState(taskId, {pinned})}
              onRename={handleRename}
              onUnreadChange={(taskId, unread) => updateTaskViewState(taskId, {unread})}
              onCreate={() => {
                setTaskDraft(null)
                setSearchParams({})
                if (missingModel) setMissingModelOpen(true)
              }}
              onSelect={selectTask}
              onToggleCollapse={() => setLayoutState((current) => ({...current, taskCollapsed: true}))}
              pinnedTaskIds={pinnedTaskIds}
              selectedTaskId={selectedTaskId}
              tasks={tasks}
              unreadTaskIds={unreadTaskIds}
            />
          )}
        </aside>
        <Conversation
          busy={Boolean(mutation)}
          connectionIssue={connectionIssue}
          events={events}
          pendingMessage={pendingMessage}
          headerActions={sideButtons}
          loading={detailLoading}
          onApproval={handleApproval}
          onCancel={handleCancel}
          onComplete={handleComplete}
          onRefresh={() => {
            setDetailLoading(true)
            Promise.all([refreshSelected(), refreshIndex()])
              .catch((reason) => setConnectionIssue((reason as Error).message))
              .finally(() => setDetailLoading(false))
          }}
          onRetry={handleRetry}
          onSend={handleSend}
          task={selectedTask}
          createEnabled={createEnabled}
          missingModel={missingModel}
          createReason={createReason}
          models={modelSettings?.models || []}
          manualDefault={manualDefault}
          defaultMode={commandMode}
          initialDraft={taskDraft}
          onCreate={handleCreate}
          onRuntimeControls={handleRuntimeControls}
          onMissingModel={() => setMissingModelOpen(true)}
        />
        <aside className="triage-context-pane">
          {layoutState.contextCollapsed ? (
            <div className="triage-collapsed-rail">
              <Button aria-label="展开任务上下文" onClick={() => setLayoutState((current) => ({...current, contextCollapsed: false}))} size="icon" title="展开任务上下文" variant="ghost">
                <PanelRightOpen />
              </Button>
            </div>
          ) : (
            <TaskContext
              busy={Boolean(mutation)}
              events={events}
              onGenerateKb={handleGenerateKb}
              onGenerateWorklog={handleGenerateWorklog}
              onToggleCollapse={() => setLayoutState((current) => ({...current, contextCollapsed: true}))}
              task={selectedTask}
            />
          )}
        </aside>
      </div>

      <Sheet onOpenChange={setTaskSheetOpen} open={taskSheetOpen}>
        <SheetContent className="w-[min(88vw,340px)] p-0" side="left">
          <SheetHeader className="sr-only">
            <SheetTitle>Ops 任务</SheetTitle>
            <SheetDescription>选择或创建人工 Ops 任务</SheetDescription>
          </SheetHeader>
          <TaskList
            createEnabled={newTaskEntryEnabled}
            createReason={createReason}
            loading={indexLoading}
            onArchive={(taskId, archived) => void handleArchive(taskId, archived)}
            onPin={(taskId, pinned) => updateTaskViewState(taskId, {pinned})}
            onRename={handleRename}
            onUnreadChange={(taskId, unread) => updateTaskViewState(taskId, {unread})}
            onCreate={() => {
              setTaskDraft(null)
              setTaskSheetOpen(false)
              setSearchParams({})
              if (missingModel) setMissingModelOpen(true)
            }}
            onSelect={selectTask}
            pinnedTaskIds={pinnedTaskIds}
            selectedTaskId={selectedTaskId}
            tasks={tasks}
            unreadTaskIds={unreadTaskIds}
          />
        </SheetContent>
      </Sheet>
      <Sheet onOpenChange={setContextSheetOpen} open={contextSheetOpen}>
        <SheetContent className="w-[min(88vw,360px)] p-0" side="right">
          <SheetHeader className="sr-only">
            <SheetTitle>任务上下文</SheetTitle>
            <SheetDescription>查看当前任务的平台、节点和操作活动</SheetDescription>
          </SheetHeader>
          <TaskContext
            busy={Boolean(mutation)}
            events={events}
            onGenerateKb={handleGenerateKb}
            onGenerateWorklog={handleGenerateWorklog}
            task={selectedTask}
          />
        </SheetContent>
      </Sheet>

      <Dialog onOpenChange={setMissingModelOpen} open={missingModelOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>暂时无法开始任务</DialogTitle>
            <DialogDescription>{createReason}。配置模型后即可发送当前输入的问题。</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button onClick={() => setMissingModelOpen(false)} variant="outline">继续编辑</Button>
            <Button asChild><Link to="/settings/models">前往模型设置</Link></Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
