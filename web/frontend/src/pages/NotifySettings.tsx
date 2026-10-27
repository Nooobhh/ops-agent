import { useCallback, useEffect, useState } from "react"
import { CheckCircle2, Send, XCircle } from "lucide-react"
import { MarkdownView } from "@/components/MarkdownView"
import { SettingsPageLayout } from "@/components/SettingsLayout"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Toggle } from "@/components/ui/toggle"
import { fetchJson, formatTime, postJson } from "@/lib/api"
import { loadNotifySettings, saveNotifySettings } from "@/lib/notifyApi"
import type { NotifyChannelStatus, NotifyChannelsResponse, NotifyProbeResponse, NotifyTemplateSettings } from "@/types"

type ChannelType = "wecom" | "feishu"
type ResultState = NotifyProbeResponse | null

const PREVIEW_VALUES: Record<string, string> = {
  kind: "资源异常",
  title: "节点 CPU 持续高负载",
  severity: "critical",
  severity_upper: "CRITICAL",
  platform: "alpha",
  target: "node1",
  host: "node1",
  service: "node_exporter",
  resource: "cpu",
  metric: "CPU 使用率",
  value: "95.0%",
  threshold: "90.0%",
  description: "最近窗口 CPU P95 为 95.0%，连续 6 个采集点处于风险区间。",
  first_seen: "2026-09-03 08:00:00",
  last_updated_at: "2026-09-03 08:05:00",
  duration: "5分钟",
  status: "异常持续",
  source: "resource_analyzer",
  alertname: "CPUHigh",
  key: "resource_forecast:hd:cpu:node1",
  run_id: "-",
  time: "2026-09-03 08:05:00",
}

function previewMarkdown(template: string) {
  return template.replace(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g, (_match, name: string) => PREVIEW_VALUES[name] || "-")
}

function Result({ result }: { result: ResultState }) {
  if (!result) return null
  return (
    <div className={`flex items-start gap-2 text-sm ${result.ok ? "text-[#2f9e6e]" : "text-destructive"}`}>
      {result.ok ? <CheckCircle2 className="mt-0.5 size-4 shrink-0" /> : <XCircle className="mt-0.5 size-4 shrink-0" />}
      <span>{result.ok ? (result.msg || "测试消息发送成功") : `${result.code === null ? "网络错误" : `code ${result.code}`}：${result.msg}`}</span>
    </div>
  )
}

function credentialPlaceholder(status: NotifyChannelStatus | undefined, fallback: string) {
  return status?.configured ? "输入新值替换，留空保持不变" : fallback
}

function ConfigInfo({ status, label, secretLabel, signed }: {
  status: NotifyChannelStatus | undefined
  label: string
  secretLabel?: string
  signed?: boolean
}) {
  if (!status?.configured) return null
  return (
    <div className="settings-setting-info-row bg-muted">
      <p className="text-xs font-medium text-muted-foreground">当前配置</p>
      <p className="mt-1 break-all font-mono text-sm">{label}：{status.credential_plain || status.credential_masked}</p>
      {secretLabel && status.secret_plain ? <p className="mt-1 break-all font-mono text-sm">{secretLabel}：{status.secret_plain}</p> : null}
      <p className="mt-1 text-xs text-muted-foreground">
        更新于 {formatTime(status.updated_at)}
        {signed === undefined ? "" : signed ? " · 已启用签名" : " · 未启用签名"}
      </p>
    </div>
  )
}

export default function NotifySettings() {
  const [channels, setChannels] = useState<NotifyChannelsResponse | null>(null)
  const [templateSettings, setTemplateSettings] = useState<NotifyTemplateSettings | null>(null)
  const [templateDraft, setTemplateDraft] = useState("")
  const [savedTemplate, setSavedTemplate] = useState("")
  const [templateBusy, setTemplateBusy] = useState(false)
  const [templateError, setTemplateError] = useState("")
  const [templateMessage, setTemplateMessage] = useState("")
  const [savedEnabled, setSavedEnabled] = useState<Record<ChannelType, boolean> | null>(null)
  const [wecomKey, setWecomKey] = useState("")
  const [feishuWebhook, setFeishuWebhook] = useState("")
  const [feishuSecret, setFeishuSecret] = useState("")
  const [busy, setBusy] = useState<Record<ChannelType, "save" | "test" | "clear" | null>>({ wecom: null, feishu: null })
  const [errors, setErrors] = useState<Record<ChannelType, string>>({ wecom: "", feishu: "" })
  const [results, setResults] = useState<Record<ChannelType, ResultState>>({ wecom: null, feishu: null })
  const [loadError, setLoadError] = useState("")

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setLoadError("")
      const [response, formatting] = await Promise.all([
        fetchJson<NotifyChannelsResponse>("/api/notify/channels", signal),
        loadNotifySettings(signal),
      ])
      setChannels(response)
      setSavedEnabled({ wecom: response.wecom.enabled, feishu: response.feishu.enabled })
      setTemplateSettings(formatting)
      setSavedTemplate(formatting.template || formatting.default_template)
      setTemplateDraft(formatting.template || formatting.default_template)
    } catch (reason) {
      if ((reason as Error).name !== "AbortError") setLoadError((reason as Error).message)
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => controller.abort()
  }, [load])

  function setEnabled(channel: ChannelType, enabled: boolean) {
    setChannels((current) => current ? {
      ...current,
      [channel]: { ...current[channel], enabled },
    } : current)
  }

  async function save(channel: ChannelType) {
    if (!channels || busy[channel]) return
    const body: Record<string, unknown> = { enabled: channels[channel].enabled }
    if (channel === "wecom" && wecomKey) body.webhook_key = wecomKey
    if (channel === "feishu") {
      if (feishuWebhook) body.webhook = feishuWebhook
      if (feishuSecret) body.secret = feishuSecret
    }
    try {
      setBusy((current) => ({ ...current, [channel]: "save" }))
      setErrors((current) => ({ ...current, [channel]: "" }))
      setResults((current) => ({ ...current, [channel]: null }))
      const response = await postJson<NotifyChannelsResponse>(`/api/notify/channels/${channel}`, body)
      setChannels(response)
      setSavedEnabled({ wecom: response.wecom.enabled, feishu: response.feishu.enabled })
      if (channel === "wecom") setWecomKey("")
      else { setFeishuWebhook(""); setFeishuSecret("") }
    } catch (reason) {
      setErrors((current) => ({ ...current, [channel]: (reason as Error).message }))
    } finally {
      setBusy((current) => ({ ...current, [channel]: null }))
    }
  }

  async function test(channel: ChannelType) {
    if (busy[channel]) return
    const body: Record<string, string> = {}
    if (channel === "wecom" && wecomKey) body.webhook_key = wecomKey
    if (channel === "feishu") {
      if (feishuWebhook) body.webhook = feishuWebhook
      if (feishuSecret) body.secret = feishuSecret
    }
    try {
      setBusy((current) => ({ ...current, [channel]: "test" }))
      setErrors((current) => ({ ...current, [channel]: "" }))
      setResults((current) => ({ ...current, [channel]: null }))
      const result = await postJson<NotifyProbeResponse>(`/api/notify/channels/${channel}/test`, body)
      setResults((current) => ({ ...current, [channel]: result }))
    } catch (reason) {
      setErrors((current) => ({ ...current, [channel]: (reason as Error).message }))
    } finally {
      setBusy((current) => ({ ...current, [channel]: null }))
    }
  }

  async function clearFeishuSecret() {
    if (!channels || busy.feishu) return
    try {
      setBusy((current) => ({ ...current, feishu: "clear" }))
      setErrors((current) => ({ ...current, feishu: "" }))
      const response = await postJson<NotifyChannelsResponse>("/api/notify/channels/feishu", {
        enabled: channels.feishu.enabled,
        secret: "",
      })
      setChannels(response)
      setSavedEnabled({ wecom: response.wecom.enabled, feishu: response.feishu.enabled })
      setFeishuSecret("")
    } catch (reason) {
      setErrors((current) => ({ ...current, feishu: (reason as Error).message }))
    } finally {
      setBusy((current) => ({ ...current, feishu: null }))
    }
  }

  async function saveTemplate() {
    if (!templateSettings || templateBusy) return
    try {
      setTemplateBusy(true); setTemplateError(""); setTemplateMessage("")
      const value = await saveNotifySettings(
        templateDraft === templateSettings.default_template ? "" : templateDraft,
      )
      setTemplateSettings(value)
      setSavedTemplate(value.template || value.default_template)
      setTemplateDraft(value.template || value.default_template)
      setTemplateMessage("Webhook 消息模板已保存；下一轮推送生效")
    } catch (reason) { setTemplateError((reason as Error).message) }
    finally { setTemplateBusy(false) }
  }

  const wecomDirty = Boolean(
    channels && savedEnabled
    && (channels.wecom.enabled !== savedEnabled.wecom || wecomKey.length > 0)
  )
  const feishuDirty = Boolean(
    channels && savedEnabled
    && (
      channels.feishu.enabled !== savedEnabled.feishu
      || feishuWebhook.length > 0
      || feishuSecret.length > 0
    )
  )
  const templateDirty = Boolean(
    templateSettings && templateDraft !== savedTemplate,
  )

  return (
    <SettingsPageLayout
      group="settings"
    >
      {loadError ? <p className="mb-4 text-sm text-destructive">{loadError}</p> : null}
      <div className="space-y-5">
        <Card className="settings-card">
          <CardHeader className="flex-row items-start justify-between gap-4">
            <div><CardTitle>企业微信</CardTitle><CardDescription className="mt-2">群机器人 webhook key</CardDescription></div>
            <Toggle aria-label="启用企业微信通知" disabled={!channels || Boolean(busy.wecom)} onPressedChange={(value) => setEnabled("wecom", value)} pressed={channels?.wecom.enabled ?? false} variant="outline">
              {channels?.wecom.enabled ? "已启用" : "已停用"}
            </Toggle>
          </CardHeader>
          <CardContent className="settings-card-content">
            <ConfigInfo label="Webhook key" status={channels?.wecom} />
            <div className="settings-setting-row"><div><label className="text-sm font-medium" htmlFor="wecom-key">{channels?.wecom.configured ? "修改 Webhook key" : "Webhook key"}</label><p className="mt-1 text-xs text-muted-foreground">留空 = 不修改</p></div><div className="settings-setting-control"><Input autoComplete="new-password" id="wecom-key" onChange={(event) => setWecomKey(event.target.value)} placeholder={credentialPlaceholder(channels?.wecom, "请输入企微机器人 webhook key")} type="password" value={wecomKey} /></div></div>
            {errors.wecom ? <p className="text-sm text-destructive">{errors.wecom}</p> : null}
            <Result result={results.wecom} />
            <div className="settings-action-row"><Button disabled={!channels || Boolean(busy.wecom)} onClick={() => void save("wecom")}>{busy.wecom === "save" ? "保存中…" : "保存"}</Button>{wecomDirty ? <span aria-live="polite" className="text-xs font-medium text-[#d69e2e]">未保存</span> : null}<Button disabled={(!channels?.wecom.configured && !wecomKey) || Boolean(busy.wecom)} onClick={() => void test("wecom")} variant="outline"><Send />{busy.wecom === "test" ? "发送中…" : "发送测试消息"}</Button></div>
          </CardContent>
        </Card>

        <Card className="settings-card">
          <CardHeader className="flex-row items-start justify-between gap-4">
            <div><CardTitle>飞书</CardTitle><CardDescription className="mt-2">群机器人 webhook，可选签名校验</CardDescription></div>
            <Toggle aria-label="启用飞书通知" disabled={!channels || Boolean(busy.feishu)} onPressedChange={(value) => setEnabled("feishu", value)} pressed={channels?.feishu.enabled ?? false} variant="outline">
              {channels?.feishu.enabled ? "已启用" : "已停用"}
            </Toggle>
          </CardHeader>
          <CardContent className="settings-card-content">
            <ConfigInfo label="Webhook" secretLabel="签名 secret" signed={channels?.feishu.signed} status={channels?.feishu} />
            <div className="settings-setting-row"><div><label className="text-sm font-medium" htmlFor="feishu-webhook">{channels?.feishu.configured ? "修改 Webhook" : "Webhook"}</label><p className="mt-1 text-xs text-muted-foreground">留空 = 不修改</p></div><div className="settings-setting-control"><Input autoComplete="new-password" id="feishu-webhook" onChange={(event) => setFeishuWebhook(event.target.value)} placeholder={credentialPlaceholder(channels?.feishu, "https://open.feishu.cn/open-apis/bot/v2/hook/…")} type="password" value={feishuWebhook} /></div></div>
            <div className="settings-setting-row"><div><label className="text-sm font-medium" htmlFor="feishu-secret">{channels?.feishu.signed ? "修改签名 secret" : "签名 secret"}</label><p className="mt-1 text-xs text-muted-foreground">群机器人开了签名校验才需要 · 当前{channels?.feishu.signed ? "已签名" : "未签名"}</p></div><div className="settings-setting-control"><Input autoComplete="new-password" id="feishu-secret" onChange={(event) => setFeishuSecret(event.target.value)} placeholder={channels?.feishu.signed ? "输入新值替换，留空保持不变" : "可选"} type="password" value={feishuSecret} /></div></div>
            {errors.feishu ? <p className="text-sm text-destructive">{errors.feishu}</p> : null}
            <Result result={results.feishu} />
            <div className="settings-action-row"><Button disabled={!channels || Boolean(busy.feishu)} onClick={() => void save("feishu")}>{busy.feishu === "save" ? "保存中…" : "保存"}</Button>{feishuDirty ? <span aria-live="polite" className="text-xs font-medium text-[#d69e2e]">未保存</span> : null}<Button disabled={(!channels?.feishu.configured && !feishuWebhook) || Boolean(busy.feishu)} onClick={() => void test("feishu")} variant="outline"><Send />{busy.feishu === "test" ? "发送中…" : "发送测试消息"}</Button><Button disabled={!channels?.feishu.signed || Boolean(busy.feishu)} onClick={() => void clearFeishuSecret()} variant="ghost">{busy.feishu === "clear" ? "清除中…" : "清除签名"}</Button></div>
          </CardContent>
        </Card>
      </div>
      <Card className="settings-card mt-5">
        <CardHeader>
          <CardTitle>Webhook 消息格式</CardTitle>
          <CardDescription>所有告警和资源异常统一使用当前 Markdown 模板；企微按 Markdown 推送，飞书自动转为纯文本。</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="grid gap-6">
            <div className="min-w-0 space-y-4">
              <div className="space-y-1.5">
                <label className="text-sm font-medium" htmlFor="notify-template">当前 Markdown 模板</label>
                <Textarea
                  className="min-h-[260px] font-mono text-xs leading-5"
                  id="notify-template"
                  onChange={(event) => { setTemplateDraft(event.target.value); setTemplateMessage("") }}
                  placeholder="输入 Markdown 模板"
                  value={templateDraft}
                />
                <p className="text-xs text-muted-foreground">留空或恢复默认使用内置模板；单条消息仍会遵守渠道的 4096 字节限制。</p>
              </div>
              <div className="rounded-md border bg-muted/30 p-3">
                <p className="text-sm font-medium">可用变量</p>
                <div className="mt-2 grid gap-x-5 gap-y-1 sm:grid-cols-2">
                  {(templateSettings?.variables || []).map((item) => <p className="text-xs text-muted-foreground" key={item.name}><code>{`{${item.name}}`}</code> · {item.description}</p>)}
                </div>
              </div>
              {templateError ? <p className="text-sm text-destructive">{templateError}</p> : null}
              {templateMessage ? <p className="text-sm text-[#2f9e6e]">{templateMessage}</p> : null}
              <div className="flex flex-wrap items-center gap-2">
                <Button disabled={!templateSettings || templateBusy} onClick={() => void saveTemplate()}>{templateBusy ? "保存中…" : "保存模板"}</Button>
                {templateDirty ? <span aria-live="polite" className="text-xs font-medium text-[#d69e2e]">未保存</span> : null}
                <Button disabled={!templateSettings || templateBusy || templateDraft === templateSettings?.default_template} onClick={() => { if (templateSettings) setTemplateDraft(templateSettings.default_template) }} variant="outline">恢复默认</Button>
              </div>
            </div>
            <div className="min-w-0 space-y-1.5">
              <div className="flex items-center justify-between gap-3">
                <p className="text-sm font-medium">通知预览</p>
                <span className="text-xs text-muted-foreground">实时 Markdown 渲染</span>
              </div>
              <div aria-label="通知 Markdown 预览" className="min-h-[390px] overflow-auto rounded-md border bg-background p-4">
                {templateSettings ? <MarkdownView content={previewMarkdown(templateDraft || templateSettings.default_template)} /> : <p className="text-sm text-muted-foreground">正在读取模板</p>}
              </div>
              <p className="text-xs text-muted-foreground">预览使用一条示例资源异常数据；保存后下一轮告警或资源风险推送生效。</p>
            </div>
          </div>
        </CardContent>
      </Card>
    </SettingsPageLayout>
  )
}
