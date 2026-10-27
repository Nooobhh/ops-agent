import { useEffect, useState, type FormEvent } from "react"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import {
  Field,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field"
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
  REASONING_LABELS,
  type ModelUpsertInput,
} from "@/lib/modelSettingsApi"
import type { ModelOption, ReasoningEffort } from "@/types"


const REASONING_LEVELS = Object.keys(REASONING_LABELS) as ReasoningEffort[]


export function ModelDialog({
  model,
  open,
  busy,
  error,
  onOpenChange,
  onSubmit,
}: {
  model: ModelOption | null
  open: boolean
  busy: boolean
  error: string
  onOpenChange: (open: boolean) => void
  onSubmit: (input: ModelUpsertInput) => Promise<void>
}) {
  const [title, setTitle] = useState("")
  const [baseUrl, setBaseUrl] = useState("")
  const [apiKey, setApiKey] = useState("")
  const [modelName, setModelName] = useState("")
  const [reasoningEffort, setReasoningEffort] = useState<ReasoningEffort>("none")

  useEffect(() => {
    if (!open) return
    setTitle(model?.title || "")
    setBaseUrl(model?.base_url || "")
    setApiKey("")
    setModelName(model?.model || "")
    setReasoningEffort(model?.default_reasoning_effort || "none")
  }, [model, open])

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    void onSubmit({
      title: title.trim(),
      base_url: baseUrl.trim(),
      api_key: apiKey,
      model: modelName.trim(),
      default_reasoning_effort: reasoningEffort,
    })
  }

  return (
    <Dialog onOpenChange={(next) => !busy && onOpenChange(next)} open={open}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>{model ? "编辑模型" : "添加模型"}</DialogTitle>
          <DialogDescription>
            OpenAI 兼容接口配置
          </DialogDescription>
        </DialogHeader>
        <form className="flex flex-col gap-6" onSubmit={submit}>
          <FieldGroup className="gap-5">
            <Field>
              <FieldLabel htmlFor="model-title">配置标题</FieldLabel>
              <Input
                autoFocus
                id="model-title"
                maxLength={120}
                onChange={(event) => setTitle(event.target.value)}
                required
                value={title}
              />
            </Field>
            <Field>
              <FieldLabel htmlFor="model-base-url">Base URL</FieldLabel>
              <Input
                id="model-base-url"
                onChange={(event) => setBaseUrl(event.target.value)}
                placeholder="https://api.example.com/v1"
                required
                type="url"
                value={baseUrl}
              />
            </Field>
            <Field>
              <FieldLabel htmlFor="model-api-key">API Key</FieldLabel>
              <Input
                autoComplete="new-password"
                id="model-api-key"
                onChange={(event) => setApiKey(event.target.value)}
                placeholder={model ? "输入新值替换，留空保持不变" : "请输入 API Key"}
                required={!model}
                type="password"
                value={apiKey}
              />
              {model ? (
                <FieldDescription>留空保持当前配置</FieldDescription>
              ) : null}
            </Field>
            <Field>
              <FieldLabel htmlFor="model-name">Model</FieldLabel>
              <Input
                id="model-name"
                onChange={(event) => setModelName(event.target.value)}
                placeholder="gpt-5"
                required
                value={modelName}
              />
            </Field>
            <Field>
              <FieldLabel htmlFor="model-default-reasoning">默认思考强度</FieldLabel>
              <Select
                onValueChange={(value: ReasoningEffort) => setReasoningEffort(value)}
                value={reasoningEffort}
              >
                <SelectTrigger id="model-default-reasoning">
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
            </Field>
            {error ? <FieldError>{error}</FieldError> : null}
          </FieldGroup>
          <DialogFooter>
            <Button
              disabled={busy}
              onClick={() => onOpenChange(false)}
              type="button"
              variant="outline"
            >
              取消
            </Button>
            <Button disabled={busy} type="submit">
              {busy ? "保存中…" : "保存"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
