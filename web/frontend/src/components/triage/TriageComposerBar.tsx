import { type FormEvent, useEffect, useRef, useState } from "react"
import { CornerDownLeft, LoaderCircle } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { REASONING_LABELS } from "@/lib/modelSettingsApi"
import {
  COMMAND_MODE_LABELS,
  composerSendEnabled,
  runtimeControlsChanged,
  shouldSubmitComposer,
  taskDialogDefaultKey,
  TRIAGE_TARGET_LABELS,
} from "@/lib/triageView"
import type {
  AlertTaskContext,
  CommandMode,
  CreateTriageTaskRequest,
  ModelOption,
  ReasoningEffort,
  RuntimeControlUpdates,
  TriageTask,
  TriageTaskDetail,
  TriageTarget,
  WorkflowModelDefault,
} from "@/types"
import { Textarea } from "@/components/ui/textarea"

export type ComposerDraft = {
  message?: string
  platform?: string
  node?: string
  alert_context?: AlertTaskContext
  work_item?: {id: string; title: string}
}

type TriageComposerBarProps = {
  task: TriageTask | null
  initialDraft?: ComposerDraft | null
  models: ModelOption[]
  manualDefault: WorkflowModelDefault | null
  defaultMode: CommandMode
  busy: boolean
  sendEnabled: boolean
  placeholder: string
  createEnabled: boolean
  missingModel: boolean
  createReason: string
  onCreate: (request: CreateTriageTaskRequest) => Promise<void>
  onSend: (message: string) => Promise<void>
  focusRequest?: number
  onRuntimeControls: (taskId: string, updates: RuntimeControlUpdates) => Promise<TriageTaskDetail>
  onMissingModel: () => void
}

export function TriageComposerBar({
  task,
  initialDraft,
  models,
  manualDefault,
  defaultMode,
  busy,
  sendEnabled,
  placeholder,
  createEnabled,
  missingModel,
  createReason,
  onCreate,
  onSend,
  focusRequest = 0,
  onRuntimeControls,
  onMissingModel,
}: TriageComposerBarProps) {
  const [message, setMessage] = useState("")
  const [platform, setPlatform] = useState<"none" | TriageTarget>("none")
  const [node, setNode] = useState("")
  const [modelId, setModelId] = useState("")
  const [reasoning, setReasoning] = useState<ReasoningEffort>("none")
  const [mode, setMode] = useState<CommandMode>(defaultMode)
  const [notice, setNotice] = useState("")
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const manualDefaultKey = taskDialogDefaultKey(manualDefault)

  // 换任务/换草稿才重置输入与提示;当前任务字段刷新(含 PATCH 回包)只同步控制项,
  // 否则「下一轮对话生效」一出现就被清掉,正在输入的补充信息也会被清空
  useEffect(() => {
    setMessage(initialDraft?.message || "")
    setNotice("")
  }, [initialDraft, task?.task_id])

  useEffect(() => {
    setPlatform(task?.platform || initialDraft?.platform || "none")
    setNode(task?.node || initialDraft?.node || "")
    setModelId(task?.model_profile_id || manualDefault?.model_id || "")
    setReasoning(task?.reasoning_effort || manualDefault?.reasoning_effort || "none")
    setMode(task?.command_mode || defaultMode)
  }, [defaultMode, initialDraft, manualDefaultKey, task?.command_mode, task?.model_profile_id, task?.node, task?.platform, task?.reasoning_effort, task?.task_id])

  useEffect(() => {
    if (focusRequest > 0) textareaRef.current?.focus()
  }, [focusRequest])

  const editable = !task || task.source === "manual"

  async function update(updates: RuntimeControlUpdates) {
    if (!task || !editable || !runtimeControlsChanged(task, updates)) return
    try {
      setNotice("")
      const response = await onRuntimeControls(task.task_id, updates)
      if (updates.node !== undefined) setNode(response.task.node || "")
      if (updates.platform !== undefined) {
        setPlatform(response.task.platform || "none")
      }
      setNotice("下一轮对话生效")
    } catch (reason) {
      setNotice((reason as Error).message)
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    const clean = message.trim()
    if (!clean || busy || !sendEnabled) return
    if (!task) {
      if (missingModel || !modelId) {
        onMissingModel()
        return
      }
      if (!createEnabled) {
        setNotice(createReason)
        return
      }
      const request: CreateTriageTaskRequest = {
        message: clean,
        model_id: modelId,
        reasoning_effort: reasoning,
        command_mode: mode,
        manual_artifacts: true,
      }
      if (initialDraft?.alert_context) request.alert_context = initialDraft.alert_context
      if (platform !== "none") request.platform = platform
      if (node.trim()) request.node = node.trim()
      // The message has been handed to the create request. Clear it before
      // waiting for classification so the Composer never looks like it is
      // still editing the in-flight message.
      setMessage("")
      await onCreate(request)
    } else {
      setMessage("")
      await onSend(clean)
    }
  }

  const agentRunning = busy || task?.status === "queued" || task?.status === "running"
  const sendLabel = agentRunning
    ? "Agent运行中"
    : task ? "发送补充信息" : "开始任务"

  return (
    <form className="triage-composer" onSubmit={(event) => void submit(event)}>
      <div className="mx-auto flex w-full max-w-3xl flex-col gap-2">
        {!task && initialDraft?.work_item ? (
          <p className="text-xs text-muted-foreground">建成后挂到工作项「{initialDraft.work_item.title}」</p>
        ) : null}
        <div className="relative">
          <Textarea
            aria-label={task ? "补充任务信息" : "任务需求"}
            // 起始单行高度,随内容长到 max-h 为止;右侧留出发送键的位置
            className="max-h-40 min-h-14 resize-none py-4 pr-12"
            disabled={busy}
            ref={textareaRef}
            maxLength={4000}
            onChange={(event) => setMessage(event.target.value)}
            onKeyDown={(event) => {
              if (shouldSubmitComposer(event.key, event.shiftKey, event.nativeEvent.isComposing)) {
                event.preventDefault()
                event.currentTarget.form?.requestSubmit()
              }
            }}
            placeholder={placeholder}
            rows={1}
            value={message}
          />
          <Button
            aria-label={sendLabel}
            // 朴素回车键:无实心底色,单行时视觉居中((56-32)/2 = 12px)
            className="absolute bottom-3 right-3 size-8 text-muted-foreground"
            disabled={!composerSendEnabled(
              task,
              message,
              busy,
              sendEnabled,
              createEnabled,
              missingModel,
            )}
            size="icon"
            title={sendLabel}
            type="submit"
            variant="ghost"
          >
            {busy ? <LoaderCircle className="animate-spin" /> : <CornerDownLeft />}
          </Button>
        </div>
        <div className="flex min-w-0 items-center justify-between gap-3 overflow-x-auto whitespace-nowrap" aria-label="任务运行时控制">
          <div className="flex shrink-0 items-center gap-2">
            <Select disabled={!editable || busy} onValueChange={(value) => {
              // 空串是表单回流不是用户选择,连本地值也不能动(见 runtimeControlsChanged)
              if (!value) return
              if (value === "pending") {
                setPlatform("none")
                void update({platform: null})
                return
              }
              const next = value as TriageTarget
              setPlatform(next)
              void update({platform: next})
            }} value={platform === "none" ? "pending" : platform}>
              <SelectTrigger className="h-8 w-28 text-xs" aria-label="平台"><SelectValue /></SelectTrigger>
              <SelectContent><SelectGroup>
                <SelectItem value="pending">平台待定</SelectItem>
                {(Object.entries(TRIAGE_TARGET_LABELS) as Array<[TriageTarget, string]>).map(([key, label]) => (
                  <SelectItem key={key} value={key}>{label}</SelectItem>
                ))}
              </SelectGroup></SelectContent>
            </Select>
            <Input
              aria-label="节点"
              className="h-8 w-32 text-xs"
              disabled={!editable || busy}
              onBlur={() => {
                // 值没变就不发请求:否则每次失焦都写一次并弹「下一轮对话生效」
                const next = node.trim() || null
                if (next !== (task?.node ?? null)) void update({node: next})
              }}
              onChange={(event) => setNode(event.target.value)}
              placeholder={task?.scope === "cluster" ? `集群 · 入口 ${task.node || "待定"}` : "节点名或 IP，如 node1 / 10.250.x.x"}
              value={node}
            />
          </div>
          <div className="ml-auto flex shrink-0 items-center gap-2">
            <Select disabled={!editable || busy} onValueChange={(value) => {
              if (!value) return
              setModelId(value)
              const next = models.find((item) => item.id === value)
              const nextReasoning = next?.default_reasoning_effort || reasoning
              setReasoning(nextReasoning)
              void update({model_id: value, reasoning_effort: nextReasoning})
            }} value={modelId}>
              <SelectTrigger className="h-8 w-32 text-xs" aria-label="模型"><SelectValue placeholder="模型待定" /></SelectTrigger>
              <SelectContent><SelectGroup>
                {models.map((item) => <SelectItem key={item.id} value={item.id}>{item.title}</SelectItem>)}
              </SelectGroup></SelectContent>
            </Select>
            <Select disabled={!editable || busy} onValueChange={(value: ReasoningEffort) => {
              if (!value) return
              setReasoning(value)
              void update({reasoning_effort: value})
            }} value={reasoning}>
              <SelectTrigger className="h-8 w-28 text-xs" aria-label="思考强度"><SelectValue /></SelectTrigger>
              <SelectContent><SelectGroup>
                {(Object.keys(REASONING_LABELS) as ReasoningEffort[]).map((item) => <SelectItem key={item} value={item}>{REASONING_LABELS[item]}</SelectItem>)}
              </SelectGroup></SelectContent>
            </Select>
            <Select disabled={!editable || busy} onValueChange={(value: CommandMode) => {
              if (!value) return
              setMode(value)
              void update({command_mode: value})
            }} value={mode}>
              <SelectTrigger className="h-8 w-24 text-xs" aria-label="权限档位"><SelectValue /></SelectTrigger>
              <SelectContent><SelectGroup>
                {(Object.keys(COMMAND_MODE_LABELS) as CommandMode[]).map((item) => (
                  <SelectItem key={item} value={item}>{COMMAND_MODE_LABELS[item]}</SelectItem>
                ))}
              </SelectGroup></SelectContent>
            </Select>
          </div>
        </div>
        {notice || task?.source === "auto" ? (
          <p aria-live="polite" className="min-w-0 text-xs text-muted-foreground">
            {notice || "自动任务不能修改控制项"}
          </p>
        ) : null}
      </div>
    </form>
  )
}
