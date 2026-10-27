import { useCallback, useEffect, useState } from "react"
import { useLocation } from "react-router-dom"
import { SettingsPageLayout } from "@/components/SettingsLayout"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
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
import {
  loadModelSettings,
  REASONING_LABELS,
  updateAgentPolicies,
} from "@/lib/modelSettingsApi"
import { loadCommandPolicy, updateCommandPolicy } from "@/lib/triageApi"
import type {
  AgentTypePolicy,
  CommandMode,
  ModelSettingsResponse,
  ReasoningEffort,
  TaskType,
} from "@/types"


const REASONING_LEVELS = Object.keys(REASONING_LABELS) as ReasoningEffort[]
const TASK_TYPES: Array<[TaskType, string]> = [
  ["qa", "问答 Agent"],
  ["investigation", "调查排查 Agent"],
  ["alert", "精准告警 Agent"],
  ["artifact", "任务产物 Agent"],
  ["default", "通用 Agent"],
]


export default function AgentSettings() {
  const location = useLocation()
  const activeSection = location.pathname.endsWith("/policies") ? "policies" : "permissions"
  const [settings, setSettings] = useState<ModelSettingsResponse | null>(null)
  const [draftPolicies, setDraftPolicies] = useState<Record<TaskType, AgentTypePolicy> | null>(null)
  const [commandMode, setCommandMode] = useState<CommandMode>("whitelist")
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState("")
  const [mutationError, setMutationError] = useState("")
  const [saved, setSaved] = useState("")
  const [busy, setBusy] = useState(false)

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setLoadError("")
      const [response, policy] = await Promise.all([
        loadModelSettings(signal),
        loadCommandPolicy(signal),
      ])
      setSettings(response)
      setDraftPolicies(response.agent_policies || null)
      setCommandMode(policy.mode)
    } catch (reason) {
      if ((reason as Error).name !== "AbortError") setLoadError((reason as Error).message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => controller.abort()
  }, [load])

  function changeCommandMode(value: CommandMode) {
    setCommandMode(value)
    setBusy(true)
    setMutationError("")
    setSaved("")
    void updateCommandPolicy(value)
      .then(() => setSaved("默认权限档位已保存"))
      .catch((reason: unknown) => setMutationError((reason as Error).message))
      .finally(() => setBusy(false))
  }

  function updateAgentPolicy(taskType: TaskType, change: Partial<AgentTypePolicy>) {
    setDraftPolicies((current) => current ? {
      ...current,
      [taskType]: {...current[taskType], ...change},
    } : current)
    setSaved("")
  }

  async function saveAgentPolicies() {
    if (!draftPolicies || busy) return
    try {
      setBusy(true)
      setMutationError("")
      const response = await updateAgentPolicies(draftPolicies)
      setSettings(response)
      setDraftPolicies(response.agent_policies || null)
      setSaved("Agent 类型策略已保存")
    } catch (reason) {
      setMutationError((reason as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <SettingsPageLayout
      group="agent"
    >
      {loadError ? (
        <Alert className="mb-5" variant="destructive">
          <AlertTitle>加载失败</AlertTitle>
          <AlertDescription>{loadError}</AlertDescription>
        </Alert>
      ) : null}
      {settings?.error ? (
        <Alert className="mb-5" variant="destructive">
          <AlertTitle>模型配置无效</AlertTitle>
          <AlertDescription>{settings.error}</AlertDescription>
        </Alert>
      ) : null}
      {mutationError ? <p className="mb-4 text-sm text-destructive" role="alert">{mutationError}</p> : null}
      {saved ? <p aria-live="polite" className="mb-4 text-sm text-[#247a56]">{saved}</p> : null}

      {activeSection === "permissions" ? <section aria-labelledby="command-mode" className="settings-section">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h2 className="text-base font-semibold" id="command-mode">全局默认权限档位</h2>
            <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
              新建任务默认跟随此档位；已有任务可在对话输入框的 chip 中单独覆盖。
            </p>
          </div>
          <Select disabled={busy || loading} onValueChange={(value: CommandMode) => changeCommandMode(value)} value={commandMode}>
            <SelectTrigger aria-label="全局默认权限档位" className="w-40"><SelectValue /></SelectTrigger>
            <SelectContent><SelectGroup>
              <SelectItem value="whitelist">default</SelectItem>
              <SelectItem value="auto">auto</SelectItem>
              <SelectItem value="bypass">bypass</SelectItem>
            </SelectGroup></SelectContent>
          </Select>
        </div>
        <p className="mt-3 text-xs text-muted-foreground">
          {commandMode === "whitelist" ? "仅收窄正表内可静态验证的只读工具。" : commandMode === "auto" ? "收宽工具集，未登记命令交审核模型与人工裁决。" : "收宽工具集，未登记命令直接执行，但红线写操作与凭据路径仍需人工审核。"}
        </p>
      </section> : null}

      {activeSection === "policies" ? <section aria-labelledby="agent-policies">
        <div className="mb-4">
          <h2 className="text-base font-semibold" id="agent-policies">Agent 类型策略</h2>
          <p className="mt-1 text-xs text-muted-foreground">
            按任务类型选择模型、思考强度、最大工具轮次和单次模型请求超时。新任务创建时固化策略。
          </p>
        </div>
        {loading ? (
          <div className="border-y py-8 text-center text-sm text-muted-foreground">正在加载</div>
        ) : settings && draftPolicies && settings.models.length > 0 ? (
          <div className="flex flex-col border-y">
            {TASK_TYPES.map(([taskType, label]) => {
              const policy = draftPolicies[taskType]
              return (
                <div className="model-default-row agent-policy-row" key={taskType}>
                  <div>
                    <p className="font-medium">{label}</p>
                    <p className="text-xs text-muted-foreground">{taskType}</p>
                  </div>
                  <Select disabled={busy} onValueChange={(model_id) => updateAgentPolicy(taskType, {model_id})} value={policy.model_id}>
                    <SelectTrigger aria-label={`${label}模型`}><SelectValue /></SelectTrigger>
                    <SelectContent><SelectGroup>
                      {settings.models.map((model) => <SelectItem key={model.id} value={model.id}>{model.title} · {model.model}</SelectItem>)}
                    </SelectGroup></SelectContent>
                  </Select>
                  <Select disabled={busy} onValueChange={(reasoning_effort: ReasoningEffort) => updateAgentPolicy(taskType, {reasoning_effort})} value={policy.reasoning_effort}>
                    <SelectTrigger aria-label={`${label}思考强度`}><SelectValue /></SelectTrigger>
                    <SelectContent><SelectGroup>
                      {REASONING_LEVELS.map((effort) => <SelectItem key={effort} value={effort}>{REASONING_LABELS[effort]}</SelectItem>)}
                    </SelectGroup></SelectContent>
                  </Select>
                  <div className="flex items-center gap-2">
                    <Input aria-label={`${label}最大工具轮次`} className="w-24" disabled={busy} max={64} min={1} onChange={(event) => {
                      const max_tool_turns = Number(event.target.value)
                      if (Number.isInteger(max_tool_turns)) updateAgentPolicy(taskType, {max_tool_turns})
                    }} step={1} type="number" value={policy.max_tool_turns} />
                    <span className="shrink-0 text-xs text-muted-foreground">轮</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <Input aria-label={`${label}模型超时秒数`} className="w-24" disabled={busy} max={600} min={1} onChange={(event) => {
                      const model_timeout_seconds = Number(event.target.value)
                      if (Number.isFinite(model_timeout_seconds)) updateAgentPolicy(taskType, {model_timeout_seconds})
                    }} step={1} type="number" value={policy.model_timeout_seconds} />
                    <span className="shrink-0 text-xs text-muted-foreground">秒</span>
                  </div>
                </div>
              )
            })}
            <div className="flex justify-end py-4">
              <Button disabled={busy} onClick={() => void saveAgentPolicies()}>{busy ? "保存中…" : "保存 Agent 策略"}</Button>
            </div>
          </div>
        ) : (
          <div className="border-y py-8 text-sm text-muted-foreground">请先添加模型</div>
        )}
      </section> : null}
    </SettingsPageLayout>
  )
}
