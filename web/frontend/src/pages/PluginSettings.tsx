import { useCallback, useEffect, useState } from "react"
import { Pencil, Plus, RefreshCw, RotateCcw, Save, TestTube2, Trash2 } from "lucide-react"
import { SettingsPageLayout } from "@/components/SettingsLayout"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Switch } from "@/components/ui/switch"
import {
  CREDENTIAL_FIELDS,
  HEALTH_LABELS,
  createPlugin,
  deletePlugin,
  loadPluginHealth,
  loadPlugins,
  patchPlugin,
  resetPlugin,
  savePluginApprovalSecret,
  savePluginConnections,
  savePluginCredential,
  testDraftPlugin,
  testPlugin,
} from "@/lib/pluginsApi"
import type {
  CustomPluginInput,
  PluginAuthScheme,
  PluginConnectionInput,
  PluginProbe,
  PluginCredentialInput,
  PluginHealth,
  PluginsResponse,
  PluginTestResponse,
  PluginView,
} from "@/types"

const AUTH_LABELS: Record<PluginAuthScheme, string> = {
  none: "无需认证", basic: "账号密码(Basic)", bearer: "Bearer Token", header: "自定义请求头", connections: "连接列表",
}
// 自定义插件可选的认证方式;连接列表只用于环境包声明的官方插件
const CUSTOM_AUTH: PluginAuthScheme[] = ["none", "basic", "bearer", "header"]
const FEED_LABELS: Record<string, string> = { alerts: "告警", resources: "资源", schedule: "流程调度拓扑" }
const EMPTY_CONNECTION: PluginConnectionInput = { name: "", host: "", port: 3306, user: "", password: "", database: "", platform: "" }
const EMPTY_DRAFT: CustomPluginInput = { id: "", title: "", url: "", auth: "none", header_name: "", cluster: "", timeout_seconds: 30 }
const SELECT_CLASS = "h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm shadow-sm outline-none focus:ring-1 focus:ring-ring"

function HealthDot({ health }: { health?: PluginHealth }) {
  if (!health) return <span className="text-xs text-muted-foreground">检测中</span>
  const ok = health.state === "ok"
  return <span className="flex items-center gap-1.5 text-xs" title={health.detail || undefined}>
    <span className={`size-2 rounded-full ${ok ? "bg-[#2f9e6e]" : "bg-[#d64545]"}`} />
    <span className={ok ? "text-muted-foreground" : "text-destructive"}>{HEALTH_LABELS[health.state]}{!ok && health.detail ? `:${health.detail}` : ""}</span>
  </span>
}

function Row({ label, help, children }: { label: string; help?: string; children: React.ReactNode }) {
  return <div className="settings-setting-row">
    <div className="min-w-0"><p className="text-sm font-medium">{label}</p>{help ? <p className="mt-1 text-xs text-muted-foreground">{help}</p> : null}</div>
    <div className="settings-setting-control col-span-2 break-all text-sm">{children}</div>
  </div>
}

function PluginCard({ plugin, health, busy, clusters, onAction }: {
  plugin: PluginView
  health?: PluginHealth
  busy: string | null
  clusters: PluginsResponse["clusters"]
  onAction: (key: string, action: () => Promise<unknown>, message?: string) => Promise<void>
}) {
  const [credential, setCredential] = useState<PluginCredentialInput>({})
  const [approvalSecret, setApprovalSecret] = useState("")
  const [result, setResult] = useState<PluginTestResponse | null>(null)
  const [editing, setEditing] = useState(false)
  const fields = CREDENTIAL_FIELDS[plugin.auth]
  const official = plugin.source === "official"
  const disabled = Boolean(busy)

  async function runTest() {
    await onAction(`test-${plugin.id}`, async () => setResult(await testPlugin(plugin.id)))
  }

  async function toggle(enabled: boolean) {
    await onAction(`enable-${plugin.id}`, async () => {
      await patchPlugin(plugin.id, { enabled })
      // 首次启用还没有工具清单:顺手做一次连接测试,免得任务开场才发现没工具
      if (enabled && !plugin.fetched_at) setResult(await testPlugin(plugin.id))
    })
  }

  return <Card className="settings-card">
    <CardHeader>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <CardTitle className="flex flex-wrap items-center gap-2">
            {plugin.title}
            <Badge variant={official ? "secondary" : "outline"}>{official ? "官方" : "自定义"}</Badge>
            {plugin.cluster ? <Badge variant="outline">{plugin.cluster.toUpperCase()} 集群</Badge> : null}
            {plugin.scope ? <Badge title={`任务里按 ${plugin.scope.label} 自动限定到本任务集群`} variant="outline">全局 · 按 {plugin.scope.label} 区分集群</Badge> : null}
            {plugin.feeds?.length ? <Badge title="中台后台采集经此插件取数,不受本页启用开关影响" variant="outline">后台采集:{plugin.feeds.map((kind) => FEED_LABELS[kind] || kind).join("、")}</Badge> : null}
            {plugin.version ? <span className="text-xs font-normal text-muted-foreground">v{plugin.version}</span> : null}
          </CardTitle>
          <CardDescription className="mt-1 flex flex-wrap items-center gap-3"><span className="font-mono">{plugin.id}</span><HealthDot health={health} /></CardDescription>
        </div>
        <label className="flex items-center gap-2 text-sm">
          {plugin.enabled ? "已启用" : "已停用"}
          <Switch aria-label={`启用 ${plugin.title}`} checked={plugin.enabled} disabled={disabled} onCheckedChange={(value) => void toggle(value)} />
        </label>
      </div>
    </CardHeader>
    <CardContent className="settings-card-content">
      <Row label="地址" help={official && plugin.default_url !== plugin.url ? `已覆盖默认地址 ${plugin.default_url}` : undefined}>
        <span className="font-mono">{plugin.url}</span>
      </Row>
      <Row label="认证" help={plugin.auth_account || undefined}>
        {AUTH_LABELS[plugin.auth]}{plugin.header_name ? `(${plugin.header_name})` : ""}
        {plugin.auth !== "none" ? <span className="ml-2 text-xs text-muted-foreground">
          {plugin.credential.configured ? `已配置${plugin.credential.username_hint ? ` · ${plugin.credential.username_hint}` : ""}` : "未配置凭据"}
          {plugin.credential.last_test ? ` · 最近测试${plugin.credential.last_test.status === "success" ? "成功" : "失败"}` : ""}
        </span> : null}
      </Row>
      {fields.map((field) => <Row help={plugin.credential.configured ? "留空保持已保存的值" : "首次配置必填"} key={field.key} label={field.label}>
        <Input autoComplete={field.secret ? "new-password" : "username"} disabled={disabled} onChange={(event) => setCredential({ ...credential, [field.key]: event.target.value })}
          placeholder={field.secret ? "仅写入,不回显" : plugin.credential.username_hint || field.label} type={field.secret ? "password" : "text"} value={credential[field.key] || ""} />
      </Row>)}
      <div className="settings-action-row">
        {fields.length ? <Button disabled={disabled} onClick={() => void onAction(`cred-${plugin.id}`, async () => {
          await savePluginCredential(plugin.id, Object.fromEntries(Object.entries(credential).filter(([, value]) => value?.trim())))
          setCredential({})
        }, "凭据已保存")}><Save data-icon="inline-start" />保存凭据</Button> : null}
        <Button disabled={disabled} onClick={() => void runTest()} variant="outline"><TestTube2 data-icon="inline-start" />{busy === `test-${plugin.id}` ? "测试中" : "连接测试并刷新工具"}</Button>
        <Button disabled={disabled} onClick={() => setEditing(true)} variant="outline"><Pencil data-icon="inline-start" />编辑</Button>
        {official
          ? <Button disabled={disabled} onClick={() => window.confirm("清除本机对该插件的地址覆盖与开关?") && void onAction(`reset-${plugin.id}`, () => resetPlugin(plugin.id), "已恢复默认")} variant="outline"><RotateCcw data-icon="inline-start" />恢复默认</Button>
          : <Button disabled={disabled} onClick={() => window.confirm(`删除插件 ${plugin.title} 及其凭据?`) && void onAction(`delete-${plugin.id}`, () => deletePlugin(plugin.id), "插件已删除")} variant="outline"><Trash2 data-icon="inline-start" />删除</Button>}
        {result ? <span className={`text-sm ${result.ok ? "text-[#2f9e6e]" : "text-destructive"}`} role="status">{result.message}</span> : null}
      </div>
      {plugin.auth === "connections" ? <ConnectionsEditor clusters={clusters} disabled={disabled} onAction={onAction} plugin={plugin} probe={result?.probe || null} /> : null}
      {plugin.write ? <>
        <Row label="写操作" help="计划工具对模型可见,调用即提出写操作;人工批准后由 Harness 签发令牌执行,模型拿不到令牌">
          {plugin.write.approval_secret_configured ? "审批密钥已配置" : "未配置审批密钥:写操作计划工具不可启用"}
        </Row>
        <Row label="审批密钥" help="与插件服务目录 approval.secret 的内容相同(建议 openssl rand -hex 32);仅写入,不回显">
          <Input autoComplete="new-password" disabled={disabled} onChange={(event) => setApprovalSecret(event.target.value)}
            placeholder="仅写入,不回显" type="password" value={approvalSecret} />
        </Row>
        <div className="settings-action-row">
          <Button disabled={disabled || !approvalSecret.trim()} onClick={() => void onAction(`approval-${plugin.id}`, async () => {
            await savePluginApprovalSecret(plugin.id, approvalSecret.trim())
            setApprovalSecret("")
          }, "审批密钥已保存")}><Save data-icon="inline-start" />保存审批密钥</Button>
        </div>
      </> : null}
      {!official ? <p className="settings-setting-info-row text-xs text-muted-foreground">第三方插件:工具默认停用,只读标记由插件自述、中台无法验证;下表描述即模型看到的原文,描述或参数变化后会自动停用待复核。</p> : null}
      {plugin.tools.length ? <div className="divide-y border-t">
        {plugin.tools.map((tool) => <div className="flex items-start gap-3 px-4 py-2.5" key={tool.name}>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <span className="font-mono">{tool.name}</span>
              {tool.write_role === "plan"
                ? <Badge variant="destructive">写操作计划 · 需人工审批</Badge>
                : tool.write_role === "execute"
                  ? <Badge variant="destructive">写 · 仅 Harness 调用</Badge>
                  : <Badge variant={tool.read_only ? "secondary" : "destructive"}>{tool.read_only ? "只读" : "写"}</Badge>}
              {tool.needs_review ? <Badge variant="outline">待复核</Badge> : null}
              {tool.blocked_reason ? <span className="text-xs text-muted-foreground">{tool.blocked_reason}</span> : null}
            </div>
            <p className="mt-1 whitespace-pre-wrap break-words text-xs text-muted-foreground">{tool.model_description}</p>
          </div>
          <Switch aria-label={`启用工具 ${tool.name}`} checked={tool.enabled} disabled={disabled || Boolean(tool.blocked_reason)}
            onCheckedChange={(value) => void onAction(`tool-${plugin.id}`, () => patchPlugin(plugin.id, { tools: { [tool.name]: value } }))} />
        </div>)}
      </div> : <p className="settings-setting-info-row text-sm text-muted-foreground">还没有工具清单:保存凭据后点「连接测试并刷新工具」。</p>}
    </CardContent>
    <PluginDialog clusters={clusters} onOpenChange={setEditing} onSaved={() => onAction(`edit-${plugin.id}`, async () => undefined)} open={editing} plugin={plugin} />
  </Card>
}

function ConnectionsEditor({ plugin, clusters, disabled, probe, onAction }: {
  plugin: PluginView
  clusters: PluginsResponse["clusters"]
  disabled: boolean
  probe: PluginProbe | null
  onAction: (key: string, action: () => Promise<unknown>, message?: string) => Promise<void>
}) {
  const saved = plugin.connections || []
  const [draft, setDraft] = useState<PluginConnectionInput | null>(null)
  const [editingName, setEditingName] = useState<string | null>(null)
  const clusterName = (key: string) => clusters.find((item) => item.key === key)?.name || key
  // 已保存的连接原样回传(不带账号密码,后端沿用原值)
  const keep = (excluded: string | null) => saved.filter((item) => item.name !== excluded).map((item) => ({
    name: item.name, host: item.host, port: item.port, database: item.database || undefined, platform: item.labels.platform || "",
  }))

  async function save() {
    if (!draft) return
    const entry = { ...draft, name: draft.name.trim(), host: draft.host.trim(), database: draft.database?.trim() || undefined,
      user: draft.user?.trim() || undefined, password: draft.password || undefined }
    await onAction(`connections-${plugin.id}`, async () => {
      await savePluginConnections(plugin.id, [...keep(editingName), entry])
      setDraft(null); setEditingName(null)
    }, "连接已保存")
  }

  async function remove(name: string) {
    if (!window.confirm(`删除连接 ${name}?`)) return
    await onAction(`connections-${plugin.id}`, () => savePluginConnections(plugin.id, keep(name)), "连接已删除")
  }

  const set = (patch: Partial<PluginConnectionInput>) => setDraft((current) => ({ ...(current || EMPTY_CONNECTION), ...patch }))
  const probeOf = (name: string) => {
    const hit = probe?.connections.find((item) => item.name === name)
    const failed = probe?.failed.find((item) => item.connection === name)
    if (failed) return <span className="text-destructive">{failed.code}</span>
    if (!hit) return null
    const facts = ["version", "role", "read_only", "missing_privileges", "error"]
      .filter((key) => hit[key] !== undefined && hit[key] !== null && !(Array.isArray(hit[key]) && !(hit[key] as unknown[]).length))
      .map((key) => `${key}: ${typeof hit[key] === "object" ? JSON.stringify(hit[key]) : String(hit[key])}`)
    return <span className="text-muted-foreground">{facts.join(" · ") || "可连接"}</span>
  }

  return <>
    <Row label="连接" help="中台持有,密码只写不回显;调用时按插件声明的请求头传给插件。有的工具按库名选连接,库名以工具说明为准">
      {saved.length ? <div className="divide-y rounded-md border">
        {saved.map((item) => <div className="flex flex-wrap items-center gap-2 px-3 py-2 text-xs" key={item.name}>
          <span className="font-mono font-medium">{item.name}</span>
          <span className="font-mono">{item.host}:{item.port}</span>
          <span>{item.user_hint}</span>
          {item.database ? <Badge variant="outline">库 {item.database}</Badge> : null}
          <Badge variant="secondary">{clusterName(item.labels.platform || "")}</Badge>
          {probeOf(item.name)}
          <span className="ml-auto flex gap-1">
            <Button aria-label={`编辑连接 ${item.name}`} disabled={disabled} onClick={() => { setEditingName(item.name); setDraft({ name: item.name, host: item.host, port: item.port, user: "", password: "", database: item.database || "", platform: item.labels.platform || "" }) }} size="sm" variant="ghost"><Pencil /></Button>
            <Button aria-label={`删除连接 ${item.name}`} disabled={disabled || saved.length <= 1} onClick={() => void remove(item.name)} size="sm" title={saved.length <= 1 ? "至少保留一条连接" : undefined} variant="ghost"><Trash2 /></Button>
          </span>
        </div>)}
      </div> : <span className="text-muted-foreground">还没有连接</span>}
      {probe?.error ? <p className="mt-1 text-xs text-destructive">{probe.error.code}:{probe.error.message}</p> : null}
    </Row>
    {draft ? <div className="grid gap-2 border-t px-4 py-3 sm:grid-cols-2">
      <label className="grid gap-1 text-sm">名称(小写字母数字与连字符)<Input disabled={disabled || editingName !== null} onChange={(e) => set({ name: e.target.value })} value={draft.name} /></label>
      <label className="grid gap-1 text-sm">所属集群<select className={SELECT_CLASS} disabled={disabled} onChange={(e) => set({ platform: e.target.value })} value={draft.platform}>
        <option value="">请选择</option>
        {clusters.map((item) => <option key={item.key} value={item.key}>{item.name}</option>)}
      </select></label>
      <label className="grid gap-1 text-sm">地址(建议填 IP)<Input disabled={disabled} onChange={(e) => set({ host: e.target.value })} value={draft.host} /></label>
      <label className="grid gap-1 text-sm">端口<Input disabled={disabled} onChange={(e) => set({ port: Number(e.target.value) || 3306 })} type="number" value={draft.port} /></label>
      <label className="grid gap-1 text-sm">账号<Input autoComplete="username" disabled={disabled} onChange={(e) => set({ user: e.target.value })} placeholder={editingName ? "留空保持原值" : ""} value={draft.user || ""} /></label>
      <label className="grid gap-1 text-sm">密码<Input autoComplete="new-password" disabled={disabled} onChange={(e) => set({ password: e.target.value })} placeholder={editingName ? "留空保持原值" : "仅写入,不回显"} type="password" value={draft.password || ""} /></label>
      <label className="grid gap-1 text-sm">库名(可选)<Input disabled={disabled} onChange={(e) => set({ database: e.target.value })} value={draft.database || ""} /></label>
    </div> : null}
    <div className="settings-action-row">
      {draft
        ? <>
          <Button disabled={disabled || !draft.name.trim() || !draft.host.trim() || !draft.platform} onClick={() => void save()}><Save data-icon="inline-start" />保存连接</Button>
          <Button disabled={disabled} onClick={() => { setDraft(null); setEditingName(null) }} variant="outline">取消</Button>
        </>
        : <Button disabled={disabled || saved.length >= 32} onClick={() => { setEditingName(null); setDraft({ ...EMPTY_CONNECTION }) }} variant="outline"><Plus data-icon="inline-start" />添加连接</Button>}
    </div>
  </>
}

function PluginDialog({ open, plugin, clusters = [], onOpenChange, onSaved }: {
  open: boolean
  plugin?: PluginView
  clusters?: PluginsResponse["clusters"]
  onOpenChange: (open: boolean) => void
  onSaved: () => Promise<void>
}) {
  const official = plugin?.source === "official"
  const [draft, setDraft] = useState<CustomPluginInput>(EMPTY_DRAFT)
  const [credential, setCredential] = useState<PluginCredentialInput>({})
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!open) return
    setDraft(plugin ? { id: plugin.id, title: plugin.title, url: plugin.url, auth: plugin.auth, header_name: plugin.header_name || "",
      cluster: plugin.cluster || "", timeout_seconds: plugin.timeout_seconds } : EMPTY_DRAFT)
    setCredential({}); setMessage(null)
  }, [open, plugin])

  async function submit(kind: "test" | "save") {
    setBusy(true); setMessage(null)
    try {
      if (kind === "test") {
        const result = await testDraftPlugin({ url: draft.url, auth: draft.auth, header_name: draft.header_name || undefined,
          timeout_seconds: draft.timeout_seconds, credential })
        setMessage({ ok: result.ok, text: result.message })
        return
      }
      if (!plugin) {
        await createPlugin({ ...draft, header_name: draft.auth === "header" ? draft.header_name : undefined, cluster: draft.cluster || null })
        if (CREDENTIAL_FIELDS[draft.auth].length && Object.values(credential).some(Boolean)) await savePluginCredential(draft.id, credential)
      } else if (official) {
        await patchPlugin(plugin.id, { url: draft.url, timeout_seconds: draft.timeout_seconds })
      } else {
        await patchPlugin(plugin.id, { title: draft.title, url: draft.url, auth: draft.auth, timeout_seconds: draft.timeout_seconds,
          header_name: draft.auth === "header" ? draft.header_name : undefined, cluster: draft.cluster || "" })
      }
      await onSaved(); onOpenChange(false)
    } catch (reason) { setMessage({ ok: false, text: (reason as Error).message }) } finally { setBusy(false) }
  }

  const set = (patch: Partial<CustomPluginInput>) => setDraft({ ...draft, ...patch })
  return <Dialog onOpenChange={(next) => !busy && onOpenChange(next)} open={open}>
    <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-xl">
      <DialogHeader>
        <DialogTitle>{plugin ? `编辑 ${plugin.title}` : "添加自定义插件"}</DialogTitle>
        <DialogDescription>{official ? "官方插件只能改本机地址与超时;认证方式由环境包声明。" : "任意 MCP 服务(Streamable HTTP)。有 /manifest 的按其展示,没有的用工具注解兜底。"}</DialogDescription>
      </DialogHeader>
      <div className="grid gap-3 text-sm">
        {!plugin ? <label className="grid gap-1">插件 id(小写字母开头,工具名前缀)<Input onChange={(e) => set({ id: e.target.value })} placeholder="my-mcp" value={draft.id} /></label> : null}
        {!official ? <label className="grid gap-1">名称<Input onChange={(e) => set({ title: e.target.value })} value={draft.title} /></label> : null}
        <label className="grid gap-1">MCP 地址<Input onChange={(e) => set({ url: e.target.value })} placeholder="http://127.0.0.1:9000/mcp" value={draft.url} /></label>
        {!official ? <>
          <label className="grid gap-1">认证方式<select className={SELECT_CLASS} onChange={(e) => set({ auth: e.target.value as PluginAuthScheme })} value={draft.auth}>
            {CUSTOM_AUTH.map((value) => <option key={value} value={value}>{AUTH_LABELS[value]}</option>)}
          </select></label>
          {draft.auth === "header" ? <label className="grid gap-1">请求头名<Input onChange={(e) => set({ header_name: e.target.value })} placeholder="X-Api-Key" value={draft.header_name} /></label> : null}
          {!plugin ? CREDENTIAL_FIELDS[draft.auth].map((field) => <label className="grid gap-1" key={field.key}>{field.label}
            <Input autoComplete="new-password" onChange={(e) => setCredential({ ...credential, [field.key]: e.target.value })} type={field.secret ? "password" : "text"} value={credential[field.key] || ""} />
          </label>) : null}
          <label className="grid gap-1">绑定集群(可选)<select className={SELECT_CLASS} onChange={(e) => set({ cluster: e.target.value })} value={draft.cluster || ""}>
            <option value="">不绑定</option>
            {clusters.map((item) => <option key={item.key} value={item.key}>{item.name}</option>)}
          </select></label>
        </> : null}
        <label className="grid gap-1">调用超时(秒,5~300)<Input max={300} min={5} onChange={(e) => set({ timeout_seconds: Number(e.target.value) })} type="number" value={draft.timeout_seconds} /></label>
        {message ? <p className={message.ok ? "text-[#2f9e6e]" : "text-destructive"} role="status">{message.text}</p> : null}
      </div>
      <DialogFooter>
        {!plugin ? <Button disabled={busy || !draft.url} onClick={() => void submit("test")} variant="outline"><TestTube2 data-icon="inline-start" />连接测试</Button> : null}
        <Button disabled={busy} onClick={() => void submit("save")}><Save data-icon="inline-start" />保存</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
}

export default function PluginSettings() {
  const [response, setResponse] = useState<PluginsResponse | null>(null)
  const [health, setHealth] = useState<Record<string, PluginHealth>>({})
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState("")
  const [message, setMessage] = useState("")
  const [adding, setAdding] = useState(false)

  const refresh = useCallback(async () => {
    const [plugins, states] = await Promise.allSettled([loadPlugins(), loadPluginHealth()])
    if (plugins.status === "fulfilled") setResponse(plugins.value)
    else setError((plugins.reason as Error).message)
    if (states.status === "fulfilled") setHealth(states.value.plugins)
  }, [])

  useEffect(() => { void refresh() }, [refresh])

  async function onAction(key: string, action: () => Promise<unknown>, done?: string) {
    if (busy) return
    setBusy(key); setError(""); setMessage("")
    try { await action(); if (done) setMessage(done) } catch (reason) { setError((reason as Error).message) } finally {
      await refresh(); setBusy(null)
    }
  }

  return <SettingsPageLayout group="plugins">
    {error ? <Alert className="mb-5" variant="destructive"><AlertDescription>{error}</AlertDescription></Alert> : null}
    {message ? <Alert className="mb-5"><AlertDescription>{message}</AlertDescription></Alert> : null}
    <div className="settings-notice-row">
      <span className="text-muted-foreground">插件 = MCP 服务。只读工具按开关交给 Agent(新任务生效);写操作只开放官方插件的计划工具,人工批准后由 Harness 执行。凭据与审批密钥只写不回显。</span>
      <div className="flex shrink-0 gap-2">
        <Button disabled={Boolean(busy)} onClick={() => void refresh()} variant="outline"><RefreshCw data-icon="inline-start" />刷新状态</Button>
        <Button disabled={Boolean(busy)} onClick={() => setAdding(true)}><Plus data-icon="inline-start" />添加插件</Button>
      </div>
    </div>
    <div className="space-y-5">
      {response?.plugins.map((plugin) => <PluginCard busy={busy} clusters={response.clusters} health={health[plugin.id]} key={plugin.id} onAction={onAction} plugin={plugin} />)}
      {response && !response.plugins.length ? <p className="text-sm text-muted-foreground">环境包没有声明插件,也还没有自定义插件。</p> : null}
    </div>
    <PluginDialog clusters={response?.clusters} onOpenChange={setAdding} onSaved={refresh} open={adding} />
  </SettingsPageLayout>
}
