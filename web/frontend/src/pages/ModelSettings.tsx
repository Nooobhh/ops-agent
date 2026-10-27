import { useCallback, useEffect, useState } from "react"
import { Pencil, Plus, Trash2 } from "lucide-react"
import { ModelDialog } from "@/components/settings/ModelDialog"
import { SettingsPageLayout } from "@/components/SettingsLayout"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Input } from "@/components/ui/input"
import {
  createModel,
  deleteModel,
  loadModelSettings,
  REASONING_LABELS,
  updateDefaults,
  updateModel,
  type ModelUpsertInput,
} from "@/lib/modelSettingsApi"
import { deleteBlocker } from "@/lib/modelSettingsView"
import type {
  ModelOption,
  ModelSettingsResponse,
  ReasoningEffort,
  WorkflowModelDefault,
} from "@/types"


const REASONING_LEVELS = Object.keys(REASONING_LABELS) as ReasoningEffort[]


export default function ModelSettings() {
  const [settings, setSettings] = useState<ModelSettingsResponse | null>(null)
  const [draftDefaults, setDraftDefaults] = useState<{
    auto: WorkflowModelDefault
    manual: WorkflowModelDefault
    weekly: WorkflowModelDefault
    reviewer?: WorkflowModelDefault | null
  } | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState("")
  const [mutationError, setMutationError] = useState("")
  const [saved, setSaved] = useState("")
  const [busy, setBusy] = useState(false)
  const [dialogOpen, setDialogOpen] = useState(false)
  const [editing, setEditing] = useState<ModelOption | null>(null)

  const applySettings = useCallback((response: ModelSettingsResponse) => {
    setSettings(response)
    setDraftDefaults(response.defaults)
  }, [])

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setLoadError("")
      const response = await loadModelSettings(signal)
      applySettings(response)
    } catch (reason) {
      if ((reason as Error).name !== "AbortError") {
        setLoadError((reason as Error).message)
      }
    } finally {
      setLoading(false)
    }
  }, [applySettings])

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => controller.abort()
  }, [load])

  function openCreate() {
    setEditing(null)
    setMutationError("")
    setSaved("")
    setDialogOpen(true)
  }

  function openEdit(model: ModelOption) {
    setEditing(model)
    setMutationError("")
    setSaved("")
    setDialogOpen(true)
  }

  async function saveModel(input: ModelUpsertInput) {
    try {
      setBusy(true)
      setMutationError("")
      const response = editing
        ? await updateModel(editing.id, input)
        : await createModel(input)
      applySettings(response)
      setDialogOpen(false)
      setSaved("模型配置已保存")
    } catch (reason) {
      setMutationError((reason as Error).message)
    } finally {
      setBusy(false)
    }
  }

  async function remove(model: ModelOption) {
    if (!window.confirm(`确认删除模型“${model.title}”？`)) return
    try {
      setBusy(true)
      setMutationError("")
      setSaved("")
      applySettings(await deleteModel(model.id))
      setSaved("模型已删除")
    } catch (reason) {
      setMutationError((reason as Error).message)
    } finally {
      setBusy(false)
    }
  }

  function updateWorkflow(
    source: "auto" | "manual" | "weekly",
    change: Partial<WorkflowModelDefault>,
  ) {
    setDraftDefaults((current) => current ? {
      ...current,
      [source]: {...current[source], ...change},
    } : current)
    setSaved("")
  }

  function updateReviewer(model_id: string) {
    setDraftDefaults((current) => {
      if (!current) return current
      if (model_id === "none") return {...current, reviewer: null}
      const profile = settings?.models.find((item) => item.id === model_id)
      return {
        ...current,
        reviewer: {
          model_id,
          reasoning_effort: profile?.default_reasoning_effort || "none",
          timeout_seconds: current.reviewer?.timeout_seconds || 120,
        },
      }
    })
    setSaved("")
  }

  async function saveDefaults() {
    if (!draftDefaults) return
    try {
      setBusy(true)
      setMutationError("")
      applySettings(await updateDefaults(draftDefaults))
      setSaved("默认模型配置已保存")
    } catch (reason) {
      setMutationError((reason as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <SettingsPageLayout group="settings">

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
      {mutationError && !dialogOpen ? (
        <p className="mb-4 text-sm text-destructive" role="alert">{mutationError}</p>
      ) : null}
      {saved ? (
        <p aria-live="polite" className="mb-4 text-sm text-[#247a56]">{saved}</p>
      ) : null}

      <section aria-labelledby="configured-models">
        <div className="mb-3 flex items-center justify-between gap-4">
          <h2 className="text-base font-semibold" id="configured-models">已配置模型</h2>
          <div className="flex items-center gap-3">
            {settings ? <span className="text-xs text-muted-foreground">{settings.models.length} 个</span> : null}
            <Button disabled={busy || Boolean(settings?.error)} onClick={openCreate} size="sm">
              <Plus data-icon="inline-start" />
              添加模型
            </Button>
          </div>
        </div>
        {loading ? (
          <div className="border-y py-10 text-center text-sm text-muted-foreground">
            正在加载
          </div>
        ) : settings && settings.models.length > 0 ? (
          <div className="border-y">
            <div className="model-settings-row model-settings-row-header" aria-hidden="true">
              <span>标题</span>
              <span>Model</span>
              <span>Base URL</span>
              <span>API Key / 强度</span>
              <span>操作</span>
            </div>
            {settings.models.map((model) => {
              const blocker = deleteBlocker(settings, model.id)
              return (
                <div className="model-settings-row" key={model.id}>
                  <div className="min-w-0">
                    <p className="truncate font-medium">{model.title}</p>
                  </div>
                  <code className="min-w-0 truncate text-xs">{model.model}</code>
                  <span className="min-w-0 truncate text-xs text-muted-foreground" title={model.base_url}>
                    {model.base_url}
                  </span>
                  <div className="flex min-w-0 flex-wrap items-center gap-2">
                    <Badge variant="secondary">
                      {model.api_key_configured ? "已配置" : "未配置"}
                    </Badge>
                    <span className="text-xs text-muted-foreground">
                      {REASONING_LABELS[model.default_reasoning_effort]}
                    </span>
                  </div>
                  <div className="flex items-center justify-end gap-1">
                    <Button
                      aria-label={`编辑 ${model.title}`}
                      disabled={busy}
                      onClick={() => openEdit(model)}
                      size="icon"
                      title="编辑模型"
                      variant="ghost"
                    >
                      <Pencil />
                    </Button>
                    <Button
                      aria-label={`删除 ${model.title}`}
                      disabled={busy || Boolean(blocker)}
                      onClick={() => void remove(model)}
                      size="icon"
                      title={blocker || "删除模型"}
                      variant="ghost"
                    >
                      <Trash2 />
                    </Button>
                  </div>
                </div>
              )
            })}
          </div>
        ) : (
          <div className="border-y py-10 text-center text-sm text-muted-foreground">
            暂无模型配置
          </div>
        )}
      </section>

      <section aria-labelledby="workflow-defaults" className="mt-8">
        <div className="mb-4">
          <h2 className="text-base font-semibold" id="workflow-defaults">默认模型</h2>
          <p className="mt-1 text-xs text-muted-foreground">分别设置各工作流的模型、思考强度和单次请求超时（1-600 秒）。</p>
        </div>
        {settings && draftDefaults && settings.models.length > 0 ? (
          <div className="flex flex-col border-y">
            {([
              ["auto", "告警自动排查"],
              ["manual", "手动排查"],
              ["weekly", "周报生成"],
            ] as const).map(([source, label]) => (
              <div className="model-default-row" key={source}>
                <div>
                  <p className="font-medium">{label}</p>
                </div>
                <Select
                  onValueChange={(model_id) => updateWorkflow(source, {model_id})}
                  value={draftDefaults[source].model_id}
                >
                  <SelectTrigger aria-label={`${label}模型`}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectGroup>
                      {settings.models.map((model) => (
                        <SelectItem key={model.id} value={model.id}>
                          {model.title} · {model.model}
                        </SelectItem>
                      ))}
                    </SelectGroup>
                  </SelectContent>
                </Select>
                <Select
                  onValueChange={(reasoning_effort: ReasoningEffort) => (
                    updateWorkflow(source, {reasoning_effort})
                  )}
                  value={draftDefaults[source].reasoning_effort}
                >
                  <SelectTrigger aria-label={`${label}思考强度`}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectGroup>
                      {REASONING_LEVELS.map((effort) => (
                        <SelectItem key={effort} value={effort}>
                          {REASONING_LABELS[effort]}
                        </SelectItem>
                      ))}
                    </SelectGroup>
                  </SelectContent>
                </Select>
                <div className="flex items-center gap-2">
                  <Input
                    aria-label={`${label}超时秒数`}
                    className="w-24"
                    max={600}
                    min={1}
                    onChange={(event) => {
                      const timeout_seconds = Number(event.target.value)
                      if (Number.isFinite(timeout_seconds)) updateWorkflow(source, {timeout_seconds})
                    }}
                    step={1}
                    type="number"
                    value={draftDefaults[source].timeout_seconds}
                  />
                  <span className="shrink-0 text-xs text-muted-foreground">秒</span>
                </div>
              </div>
            ))}
            <div className="model-default-row">
              <div>
                <p className="font-medium">auto 审核模型</p>
                <p className="text-xs text-muted-foreground">
                  可选；未配置时未知命令全部转人工审批
                </p>
              </div>
              <Select
                onValueChange={updateReviewer}
                value={draftDefaults.reviewer?.model_id || "none"}
              >
                <SelectTrigger aria-label="auto 审核模型">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectGroup>
                    <SelectItem value="none">未配置（转人工）</SelectItem>
                    {settings.models.map((model) => (
                      <SelectItem key={model.id} value={model.id}>
                        {model.title} · {model.model}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                </SelectContent>
              </Select>
              <Select
                disabled={!draftDefaults.reviewer}
                onValueChange={(reasoning_effort: ReasoningEffort) => {
                  setDraftDefaults((current) => current && current.reviewer ? {
                    ...current,
                    reviewer: {...current.reviewer, reasoning_effort},
                  } : current)
                  setSaved("")
                }}
                value={draftDefaults.reviewer?.reasoning_effort || "none"}
              >
                <SelectTrigger aria-label="auto 审核模型思考强度">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectGroup>
                    {REASONING_LEVELS.map((effort) => (
                      <SelectItem key={effort} value={effort}>
                        {REASONING_LABELS[effort]}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                </SelectContent>
              </Select>
              <div className="flex items-center gap-2">
                <Input
                  aria-label="auto审核模型超时秒数"
                  className="w-24"
                  disabled={!draftDefaults.reviewer}
                  max={600}
                  min={1}
                  onChange={(event) => {
                    const timeout_seconds = Number(event.target.value)
                    if (Number.isFinite(timeout_seconds)) {
                      setDraftDefaults((current) => current && current.reviewer ? {
                        ...current,
                        reviewer: {...current.reviewer, timeout_seconds},
                      } : current)
                      setSaved("")
                    }
                  }}
                  step={1}
                  type="number"
                  value={draftDefaults.reviewer?.timeout_seconds || 120}
                />
                <span className="shrink-0 text-xs text-muted-foreground">秒</span>
              </div>
            </div>
            <div className="flex justify-end py-4">
              <Button disabled={busy} onClick={() => void saveDefaults()}>
                {busy ? "保存中…" : "保存默认设置"}
              </Button>
            </div>
          </div>
        ) : (
          <div className="border-y py-8 text-sm text-muted-foreground">
            请先添加模型
          </div>
        )}
      </section>

      <ModelDialog
        busy={busy}
        error={dialogOpen ? mutationError : ""}
        model={editing}
        onOpenChange={setDialogOpen}
        onSubmit={saveModel}
        open={dialogOpen}
      />
    </SettingsPageLayout>
  )
}
