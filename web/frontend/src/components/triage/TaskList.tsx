import { type CSSProperties, type FormEvent, useEffect, useMemo, useRef, useState } from "react"
import {
  Archive,
  ArchiveRestore,
  Bot,
  MoreHorizontal,
  PanelLeftClose,
  Pencil,
  Pin,
  PinOff,
  Plus,
  UserRound,
} from "lucide-react"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { formatTime } from "@/lib/api"
import { agentDisplayLabel, failureCause, groupForStatus, taskDisplayTitle, taskTypeLabel, triageTargetLabel, type TaskGroup } from "@/lib/triageView"
import type { TriageTask, TriageTaskSource } from "@/types"


type TaskFilter = "all" | TaskGroup | "archived"
type SourceFilter = "all" | TriageTaskSource

function TaskTitle({ text, displayTitle }: { text: string; displayTitle?: string | null }) {
  const ref = useRef<HTMLSpanElement>(null)
  const [duration, setDuration] = useState(12)

  useEffect(() => {
    const measure = () => {
      const element = ref.current
      if (!element) return
      const overflow = Math.max(0, element.scrollWidth - (element.parentElement?.clientWidth || 0))
      // The distance grows with the overflow; keep a long title readable by
      // slowing the animation instead of making it sweep faster.
      setDuration(Math.min(36, Math.max(12, 12 + overflow / 24)))
    }
    measure()
    const observer = typeof ResizeObserver === "undefined"
      ? null
      : new ResizeObserver(measure)
    if (ref.current && observer) observer.observe(ref.current)
    return () => observer?.disconnect()
  }, [text, displayTitle])

  return (
    <span
      className="triage-task-title-text"
      ref={ref}
      style={{ "--triage-title-duration": `${duration}s` } as CSSProperties}
      title={text}
    >
      {taskDisplayTitle(text, displayTitle)}
    </span>
  )
}

type TaskListProps = {
  tasks: TriageTask[]
  selectedTaskId: string | null
  createEnabled: boolean
  createReason: string
  loading: boolean
  onCreate: () => void
  onSelect: (taskId: string) => void
  onArchive: (taskId: string, archived: boolean) => void
  onRename: (taskId: string, title: string) => Promise<void>
  onPin: (taskId: string, pinned: boolean) => void
  onUnreadChange: (taskId: string, unread: boolean) => void
  pinnedTaskIds: ReadonlySet<string>
  unreadTaskIds: ReadonlySet<string>
  onToggleCollapse?: () => void
}


export function TaskList({
  tasks,
  selectedTaskId,
  createEnabled,
  createReason,
  loading,
  onCreate,
  onSelect,
  onArchive,
  onRename,
  onPin,
  onUnreadChange,
  pinnedTaskIds,
  unreadTaskIds,
  onToggleCollapse,
}: TaskListProps) {
  const [statusFilter, setStatusFilter] = useState<TaskFilter>("all")
  const [sourceFilter, setSourceFilter] = useState<SourceFilter>("all")
  const [confirmTask, setConfirmTask] = useState<TriageTask | null>(null)
  const [menuTaskId, setMenuTaskId] = useState<string | null>(null)
  const [renameTask, setRenameTask] = useState<TriageTask | null>(null)
  const [renameTitle, setRenameTitle] = useState("")
  const [renameError, setRenameError] = useState("")
  const [renameSaving, setRenameSaving] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)
  const visible = useMemo(() => tasks.filter((task) => {
    if (sourceFilter !== "all" && task.source !== sourceFilter) return false
    if (statusFilter === "archived") return Boolean(task.archived_at)
    if (task.archived_at) return false
    return statusFilter === "all" || groupForStatus(task.status) === statusFilter
  }).sort((left, right) => {
    const pinOrder = Number(pinnedTaskIds.has(right.task_id)) - Number(pinnedTaskIds.has(left.task_id))
    return pinOrder || right.created_at.localeCompare(left.created_at)
  }), [pinnedTaskIds, sourceFilter, statusFilter, tasks])
  const archivedCount = useMemo(
    () => tasks.filter((task) => task.archived_at).length,
    [tasks],
  )

  useEffect(() => {
    if (!menuTaskId) return
    const closeOutside = (event: PointerEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) setMenuTaskId(null)
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenuTaskId(null)
    }
    document.addEventListener("pointerdown", closeOutside)
    document.addEventListener("keydown", closeOnEscape)
    return () => {
      document.removeEventListener("pointerdown", closeOutside)
      document.removeEventListener("keydown", closeOnEscape)
    }
  }, [menuTaskId])

  function beginRename(task: TriageTask) {
    setMenuTaskId(null)
    setRenameTask(task)
    setRenameTitle(taskDisplayTitle(task.initial_message, task.display_title))
    setRenameError("")
  }

  async function submitRename(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!renameTask) return
    const title = renameTitle.trim()
    if (!title) {
      setRenameError("名称不能为空")
      return
    }
    setRenameSaving(true)
    setRenameError("")
    try {
      await onRename(renameTask.task_id, title)
      setRenameTask(null)
    } catch (reason) {
      setRenameError((reason as Error).message || "重命名失败")
    } finally {
      setRenameSaving(false)
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col bg-background">
      <div className="border-b p-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h2 className="text-sm font-semibold">Ops 任务</h2>
            <p className="mt-0.5 text-xs text-muted-foreground">
              {tasks.length - archivedCount} 个任务
              {archivedCount ? ` · ${archivedCount} 已归档` : ""}
            </p>
          </div>
          <div className="flex items-center gap-1">
            {onToggleCollapse ? (
              <Button
                aria-label="收起任务列表"
                onClick={onToggleCollapse}
                size="icon"
                title="收起任务列表"
                variant="ghost"
              >
                <PanelLeftClose />
              </Button>
            ) : null}
            <Button
              aria-label="新建任务"
              disabled={!createEnabled}
              onClick={onCreate}
              size="icon"
              title={createEnabled ? "新建任务" : createReason}
            >
              <Plus />
            </Button>
          </div>
        </div>
        <div className="mt-3 grid grid-cols-2 gap-2">
          <Select onValueChange={(value) => setStatusFilter(value as TaskFilter)} value={statusFilter}>
            <SelectTrigger aria-label="按任务状态筛选" className="h-8 text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectGroup>
                <SelectItem value="all">全部状态</SelectItem>
                <SelectItem value="active">进行中</SelectItem>
                <SelectItem value="waiting">待用户处理</SelectItem>
                <SelectItem value="finished">已结束</SelectItem>
                <SelectItem value="archived">已归档</SelectItem>
              </SelectGroup>
            </SelectContent>
          </Select>
          <Select onValueChange={(value) => setSourceFilter(value as SourceFilter)} value={sourceFilter}>
            <SelectTrigger aria-label="按任务来源筛选" className="h-8 text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectGroup>
                <SelectItem value="all">全部来源</SelectItem>
                <SelectItem value="manual">人工</SelectItem>
                <SelectItem value="auto">自动</SelectItem>
              </SelectGroup>
            </SelectContent>
          </Select>
        </div>
      </div>
      <ScrollArea className="min-h-0 flex-1">
        <div className="py-1">
          {visible.map((task) => {
            const selected = task.task_id === selectedTaskId
            const SourceIcon = task.source === "manual" ? UserRound : Bot
            const archived = Boolean(task.archived_at)
            const archivable = archived || groupForStatus(task.status) === "finished"
            const cause = failureCause(task)
            const ArchiveIcon = archived ? ArchiveRestore : Archive
            const pinned = pinnedTaskIds.has(task.task_id)
            const unread = unreadTaskIds.has(task.task_id)
            const menuOpen = menuTaskId === task.task_id
            return (
              <div className="triage-task-item" key={task.task_id}>
                <button
                  aria-current={selected ? "true" : undefined}
                  className="triage-task-row pr-10"
                  data-selected={selected}
                  data-unread={unread}
                  onClick={() => onSelect(task.task_id)}
                  type="button"
                >
                  <div className="flex min-w-0 items-start gap-2">
                    {unread ? <span aria-label="未读" className="triage-task-unread-dot" title="未读" /> : null}
                    <SourceIcon className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
                    <strong className="triage-task-title">
                      <TaskTitle displayTitle={task.display_title} text={task.initial_message} />
                    </strong>
                  </div>
                  <div className="mt-2 flex items-center justify-between gap-2 text-xs">
                    <span className={`triage-status triage-status-${task.status}`}>
                      {agentDisplayLabel(task.status)}
                    </span>
                    <time className="text-muted-foreground" dateTime={task.updated_at}>
                      {formatTime(task.updated_at)}
                    </time>
                  </div>
                  {cause ? (
                    <p className="mt-1 truncate text-left text-[11px] text-destructive" title={task.error || cause}>
                      {cause}
                    </p>
                  ) : null}
                  <p className="mt-1 truncate text-left text-[11px] text-muted-foreground">
                    {taskTypeLabel(task.task_type)} · {triageTargetLabel(task.platform)} · {task.scope === "cluster"
                      ? "集群"
                      : task.node || "节点待确认"}
                  </p>
                </button>
                <button
                  aria-expanded={menuOpen}
                  aria-haspopup="menu"
                  aria-label={`更多操作：${taskDisplayTitle(task.initial_message, task.display_title)}`}
                  className={`triage-task-more ${menuOpen ? "is-open" : ""}`}
                  onClick={() => setMenuTaskId(menuOpen ? null : task.task_id)}
                  title="更多操作"
                  type="button"
                >
                  <MoreHorizontal className="size-3.5" aria-hidden="true" />
                </button>
                {menuOpen ? (
                  <div className="triage-task-menu" ref={menuRef} role="menu">
                    <button onClick={() => beginRename(task)} role="menuitem" type="button">
                      <Pencil aria-hidden="true" />
                      重命名
                    </button>
                    <button
                      onClick={() => {
                        onPin(task.task_id, !pinned)
                        setMenuTaskId(null)
                      }}
                      role="menuitem"
                      type="button"
                    >
                      {pinned ? <PinOff aria-hidden="true" /> : <Pin aria-hidden="true" />}
                      {pinned ? "取消置顶" : "置顶"}
                    </button>
                    <button
                      disabled={!archivable}
                      onClick={() => {
                        if (archived) onArchive(task.task_id, false)
                        else setConfirmTask(task)
                        setMenuTaskId(null)
                      }}
                      role="menuitem"
                      title={!archivable ? "只有已结束的任务可以归档" : undefined}
                      type="button"
                    >
                      <ArchiveIcon aria-hidden="true" />
                      {archived ? "取消归档" : "归档"}
                    </button>
                    <button
                      onClick={() => {
                        onUnreadChange(task.task_id, !unread)
                        setMenuTaskId(null)
                      }}
                      role="menuitem"
                      type="button"
                    >
                      {unread ? "标记为已读" : "标记为未读"}
                    </button>
                  </div>
                ) : null}
              </div>
            )
          })}
          {!loading && visible.length === 0 ? (
            <div className="px-5 py-14 text-center text-sm text-muted-foreground">
              {statusFilter === "archived" ? "还没有归档的任务" : "当前筛选下没有任务"}
            </div>
          ) : null}
          {loading && tasks.length === 0 ? (
            <div className="px-5 py-14 text-center text-sm text-muted-foreground">
              正在读取任务
            </div>
          ) : null}
        </div>
      </ScrollArea>
      <Dialog
        onOpenChange={(open) => { if (!open) setConfirmTask(null) }}
        open={Boolean(confirmTask)}
      >
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>归档任务</DialogTitle>
            <DialogDescription className="line-clamp-4 break-words">
              「{confirmTask?.initial_message}」将从默认列表隐藏，可随时在「已归档」视图中查看或恢复。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button onClick={() => setConfirmTask(null)} type="button" variant="outline">
              取消
            </Button>
            <Button
              onClick={() => {
                if (confirmTask) onArchive(confirmTask.task_id, true)
                setConfirmTask(null)
              }}
              type="button"
            >
              <Archive data-icon="inline-start" />
              归档
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog
        onOpenChange={(open) => {
          if (!open && !renameSaving) {
            setRenameTask(null)
            setRenameError("")
          }
        }}
        open={Boolean(renameTask)}
      >
        <DialogContent className="max-w-sm">
          <form onSubmit={(event) => void submitRename(event)}>
            <DialogHeader>
              <DialogTitle>重命名任务</DialogTitle>
              <DialogDescription>只修改侧边栏显示名称，不会改变原始任务请求。</DialogDescription>
            </DialogHeader>
            <Input
              aria-label="任务名称"
              autoFocus
              className="mt-4"
              disabled={renameSaving}
              maxLength={120}
              onChange={(event) => setRenameTitle(event.target.value)}
              value={renameTitle}
            />
            {renameError ? <p className="mt-2 text-sm text-destructive">{renameError}</p> : null}
            <DialogFooter className="mt-5">
              <Button disabled={renameSaving} onClick={() => setRenameTask(null)} type="button" variant="outline">
                取消
              </Button>
              <Button disabled={renameSaving} type="submit">
                {renameSaving ? "保存中" : "保存"}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  )
}
