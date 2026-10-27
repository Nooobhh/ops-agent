import { useEffect, useState } from "react"
import { CheckCircle2, Clock3, KeyRound, RefreshCw, Save, TestTube2, XCircle } from "lucide-react"
import { SettingsPageLayout } from "@/components/SettingsLayout"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import {
  generateNodeCredentials,
  loadCredentials,
  saveCredentialProfile,
  testCredentialProfile,
  testSshConnection,
} from "@/lib/credentialsApi"
import type { CredentialProfile, CredentialTestResponse, CredentialsResponse, SshCredentialNodeStatus } from "@/types"

type ProfileForm = Record<string, string>

function ProfileStatus({ profile }: { profile: CredentialProfile | undefined }) {
  if (!profile) return null
  return <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
    <Badge variant={profile.configured ? "secondary" : "outline"}>
      {profile.configured ? "已配置" : "未配置"}
    </Badge>
    {profile.username_hint ? <span>账号：{profile.username_hint}</span> : null}
    {profile.last_test ? <span className={profile.last_test.status === "success" ? "text-[#2f9e6e]" : "text-destructive"}>
      最近测试：{profile.last_test.status === "success" ? "成功" : "失败"}
    </span> : null}
  </div>
}

function TestResult({ result }: { result: CredentialTestResponse | null }) {
  if (!result) return null
  return <div className={`flex items-center gap-2 text-sm ${result.ok ? "text-[#2f9e6e]" : "text-destructive"}`} role="status">
    {result.ok ? <CheckCircle2 className="size-4" /> : <XCircle className="size-4" />}
    <span>{result.message}</span>
  </div>
}

function formatValidity(value: number) {
  if (!value) return "未返回固定过期时间"
  return new Date(value * 1000).toLocaleString("zh-CN", { hour12: false })
}

function nodeStateLabel(node: SshCredentialNodeStatus) {
  if (node.state === "expired") return "已过期"
  if (node.state === "long_lived") return "长效"
  if (node.state === "active") return "有效"
  return "时效未知"
}

function SshCredentialStatus({ nodes }: { nodes: SshCredentialNodeStatus[] | undefined }) {
  return <div className="settings-setting-info-row bg-muted/25">
    <div className="flex items-center gap-2 text-sm font-medium"><Clock3 className="size-4" />SSH 凭证时效</div>
    <p className="mt-1 text-xs text-muted-foreground">时效仅用于展示和人工判断，不会触发自动刷新。</p>
    {!nodes?.length ? <p className="mt-3 text-sm text-muted-foreground">尚未发现节点凭证缓存。</p> : <div className="mt-3 max-h-64 space-y-2 overflow-auto pr-1">
      {nodes.map((node) => <div className="rounded border bg-background px-3 py-2 text-xs" key={node.host}>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="font-medium">{node.target || node.host} <span className="font-normal text-muted-foreground">({node.host})</span></span>
          <span className={node.state === "expired" ? "font-medium text-destructive" : node.state === "long_lived" ? "font-medium text-[#2f9e6e]" : "text-muted-foreground"}>{nodeStateLabel(node)}</span>
        </div>
        <div className="mt-1 grid gap-1 text-muted-foreground sm:grid-cols-2">
          <span>模式：{node.mode === 0 ? "长效" : node.mode === 1 ? "短效" : "未知"}</span>
          <span>有效至：{node.valid_to ? formatValidity(node.valid_to) : "未返回固定过期时间"}</span>
          <span>缓存于：{node.cached_at || "未知"}</span>
          {node.valid_from ? <span>生成于：{formatValidity(node.valid_from)}</span> : null}
        </div>
      </div>)}
    </div>}
  </div>
}

function Field({
  id,
  label,
  help,
  children,
}: {
  id: string
  label: string
  help: string
  children: React.ReactNode
}) {
  return <div className="settings-setting-row credentials-setting-row">
    <div className="min-w-0">
      <label className="text-sm font-medium" htmlFor={id}>{label}</label>
      <p className="mt-1 text-xs text-muted-foreground">{help}</p>
    </div>
    <div className="settings-setting-control">{children}</div>
  </div>
}

export default function CredentialsSettings() {
  const [response, setResponse] = useState<CredentialsResponse | null>(null)
  const [forms, setForms] = useState<Record<string, ProfileForm>>({})
  const [ssh, setSsh] = useState<{ platform: string; node: string }>({ platform: "", node: "" })
  const [busy, setBusy] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const [message, setMessage] = useState("")
  const [testResult, setTestResult] = useState<CredentialTestResponse | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    loadCredentials(controller.signal).then((value) => {
      setResponse(value)
      setSsh((current) => current.platform ? current : { ...current, platform: value.ssh_test_platforms[0]?.key || "" })
    }).catch((reason) => {
      if ((reason as Error).name !== "AbortError") setError((reason as Error).message)
    }).finally(() => setLoading(false))
    return () => controller.abort()
  }, [])

  function patchForm(profileId: string, patch: ProfileForm) {
    setForms((current) => ({ ...current, [profileId]: { ...current[profileId], ...patch } }))
  }

  async function saveProfile(profile: CredentialProfile) {
    if (busy) return
    setBusy(`save-${profile.id}`); setError(""); setMessage(""); setTestResult(null)
    try {
      const value = await saveCredentialProfile(profile.id, onlyFilled(forms[profile.id] || {}))
      setResponse(value); setForms((current) => ({ ...current, [profile.id]: {} })); setMessage(`${profile.title} 认证已保存`)
    } catch (reason) { setError((reason as Error).message) } finally { setBusy(null) }
  }

  async function testProfile(profile: CredentialProfile) {
    if (busy) return
    setBusy(`test-${profile.id}`); setError(""); setMessage(""); setTestResult(null)
    try { setTestResult(await testCredentialProfile(profile.id, onlyFilled(forms[profile.id] || {}))) } catch (reason) { setError((reason as Error).message) } finally { setBusy(null) }
  }

  async function generateNodes() {
    const provider = response?.node_credentials.provider
    if (busy || !provider) return
    if (!window.confirm(provider.generate_confirm)) return
    setBusy("generate-nodes"); setError(""); setMessage(""); setTestResult(null)
    try {
      const value = await generateNodeCredentials()
      setResponse((current) => current ? { ...current, node_credentials: value.status } : current)
      if (value.failed.length) {
        setError(`已生成 ${value.generated.length} 个节点凭证，${value.failed.length} 个节点失败`)
      } else {
        setMessage(`已手动生成 ${value.generated.length} 个节点凭证`)
      }
    } catch (reason) { setError((reason as Error).message) } finally { setBusy(null) }
  }

  async function testSsh() {
    if (busy || !ssh.node.trim() || !ssh.platform) return
    setBusy("test-ssh"); setError(""); setMessage(""); setTestResult(null)
    try { setTestResult(await testSshConnection({ platform: ssh.platform, node: ssh.node.trim() })) } catch (reason) { setError((reason as Error).message) } finally { setBusy(null) }
  }

  const provider = response?.node_credentials.provider
  return <SettingsPageLayout
    group="settings"
  >
    {error ? <Alert className="mb-5" variant="destructive"><AlertDescription>{error}</AlertDescription></Alert> : null}
    {message ? <Alert className="mb-5"><AlertDescription>{message}</AlertDescription></Alert> : null}
    <div className="space-y-5">
      {provider ? <Card className="settings-card">
        <CardHeader>
          <CardTitle className="flex items-center gap-2"><KeyRound className="size-4" />{provider.title}</CardTitle>
          <CardDescription>{provider.description}</CardDescription>
        </CardHeader>
        <CardContent className="settings-card-content">
          <SshCredentialStatus nodes={response?.node_credentials.nodes} />
          <div className="settings-action-row">
            <Button disabled={loading || Boolean(busy)} onClick={() => void generateNodes()} variant="outline"><RefreshCw data-icon="inline-start" />{busy === "generate-nodes" ? "生成中" : provider.generate_label}</Button>
          </div>
          <p className="text-xs text-muted-foreground">登录测试和 SSH 测试不会生成或刷新节点凭证，只有点击此按钮时才会显式替换。</p>
        </CardContent>
      </Card> : null}

      {(response?.profiles || []).map((profile) => <Card className="settings-card" key={profile.id}>
        <CardHeader>
          <div className="flex items-start justify-between gap-3"><div><CardTitle>{profile.title}</CardTitle><CardDescription>{profile.description}</CardDescription></div><ProfileStatus profile={profile} /></div>
        </CardHeader>
        <CardContent className="settings-card-content">
          {profile.fields.map((field) => {
            const id = `profile-${profile.id}-${field.name}`.replace(/[^A-Za-z0-9_-]/g, "-")
            const secret = field.kind === "secret"
            const help = !secret && profile.username_hint ? `当前已配置 ${profile.username_hint}；留空保持不变。` : field.help
            return <Field help={help} id={id} key={field.name} label={field.label}>
              <Input autoComplete={secret ? "new-password" : "off"} disabled={loading || Boolean(busy)} id={id} onChange={(event) => patchForm(profile.id, { [field.name]: event.target.value })} placeholder={secret ? "仅写入，不回显" : profile.username_hint || field.label} type={secret ? "password" : field.kind === "number" ? "number" : "text"} value={forms[profile.id]?.[field.name] || ""} />
            </Field>
          })}
          <div className="settings-action-row">
            <Button disabled={loading || Boolean(busy)} onClick={() => void saveProfile(profile)}><Save data-icon="inline-start" />{busy === `save-${profile.id}` ? "保存中" : "保存"}</Button>
            {profile.testable ? <Button disabled={loading || Boolean(busy)} onClick={() => void testProfile(profile)} variant="outline"><TestTube2 data-icon="inline-start" />{busy === `test-${profile.id}` ? "测试中" : "登录测试"}</Button> : null}
          </div>
          <TestResult result={testResult?.kind === "profile" && testResult.profile_id === profile.id ? testResult : null} />
        </CardContent>
      </Card>)}
      <Card className="settings-card">
        <CardHeader>
          <CardTitle>SSH 连接测试</CardTitle>
          <CardDescription>仅对平台登记节点执行固定只读命令 true，不接受自定义命令；登录测试成功后仍可用此项验证节点访问链路。</CardDescription>
        </CardHeader>
        <CardContent className="settings-card-content">
          <Field id="ssh-test-platform" label="平台" help="选择目标集群。">
            <select className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm shadow-sm outline-none focus:ring-1 focus:ring-ring" disabled={loading || Boolean(busy)} id="ssh-test-platform" onChange={(event) => setSsh({ ...ssh, platform: event.target.value })} value={ssh.platform}>
              {(response?.ssh_test_platforms || []).map((item) => <option key={item.key} value={item.key}>{item.label}</option>)}
            </select>
          </Field>
          <Field id="ssh-test-node" label="节点" help="例如 node1；必须存在于所选平台拓扑。">
            <Input disabled={loading || Boolean(busy)} id="ssh-test-node" onChange={(event) => setSsh({ ...ssh, node: event.target.value })} placeholder="node1" value={ssh.node} />
          </Field>
          <div className="settings-action-row"><Button disabled={loading || Boolean(busy) || !ssh.node.trim() || !ssh.platform} onClick={() => void testSsh()} variant="outline"><TestTube2 data-icon="inline-start" />{busy === "test-ssh" ? "测试中" : "测试 SSH"}</Button><TestResult result={testResult?.kind === "ssh" ? testResult : null} /></div>
        </CardContent>
      </Card>
    </div>
    <p className="mt-5 text-xs text-muted-foreground">认证资料由后端保存和使用，前端只接收配置状态及脱敏用户名。登录测试不会执行 SSH 命令；节点 SSH 可用性属于排查 Harness 的连接测试。</p>
  </SettingsPageLayout>
}

function onlyFilled(value: ProfileForm): ProfileForm {
  return Object.fromEntries(Object.entries(value).filter(([, item]) => item.trim()))
}
