import { useEffect, useState } from "react"
import { CheckCircle2, Link2, Save, TestTube2, XCircle } from "lucide-react"
import { useLocation } from "react-router-dom"
import { SettingsPageLayout } from "@/components/SettingsLayout"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Switch } from "@/components/ui/switch"
import { loadVectorSettings, saveVectorSettings, testVectorSettings } from "@/lib/vectorSettingsApi"
import type {
  EmbeddingBackend,
  VectorSettingsInput,
  VectorSettingsResponse,
  VectorSettingsTestResponse,
  RerankBackend,
  VectorStoreBackend,
} from "@/types"

type FormState = {
  backend: VectorStoreBackend
  embedded_path: string
  fallback_to_embedded: boolean
  timeout_seconds: number
  milvus: {
    endpoint: string
    collection: string
    vector_field: string
    token: string
  }
  embedding: {
    backend: EmbeddingBackend
    model: string
    endpoint: string
    api_key: string
    dimensions: number | null
    device: string
  }
  rerank: {
    backend: RerankBackend
    model: string
    endpoint: string
    api_key: string
    device: string
    max_candidates: number
    max_length: number
  }
}

const DEFAULT_LOCAL_EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"
const DEFAULT_LOCAL_RERANK_MODEL = "BAAI/bge-reranker-v2-m3"

function formFromResponse(response: VectorSettingsResponse): FormState {
  return {
    backend: response.settings.backend,
    embedded_path: response.settings.embedded_path,
    fallback_to_embedded: response.settings.fallback_to_embedded,
    timeout_seconds: response.settings.timeout_seconds,
    milvus: {
      endpoint: response.settings.milvus.endpoint,
      collection: response.settings.milvus.collection,
      vector_field: response.settings.milvus.vector_field,
      token: "",
    },
    embedding: {
      backend: response.settings.embedding.backend,
      model: response.settings.embedding.model,
      endpoint: response.settings.embedding.endpoint,
      api_key: "",
      dimensions: response.settings.embedding.dimensions,
      device: response.settings.embedding.device,
    },
    rerank: {
      backend: response.settings.rerank.backend,
      model: response.settings.rerank.model,
      endpoint: response.settings.rerank.endpoint,
      api_key: "",
      device: response.settings.rerank.device,
      max_candidates: response.settings.rerank.max_candidates,
      max_length: response.settings.rerank.max_length,
    },
  }
}

function payloadFromForm(form: FormState): VectorSettingsInput {
  const milvus: VectorSettingsInput["milvus"] = {
    endpoint: form.milvus.endpoint,
    collection: form.milvus.collection,
    vector_field: form.milvus.vector_field,
  }
  if (form.milvus.token.trim()) milvus.token = form.milvus.token
  const embedding: VectorSettingsInput["embedding"] = {
    backend: form.embedding.backend,
    model: form.embedding.model,
    endpoint: form.embedding.endpoint,
    dimensions: form.embedding.dimensions,
    device: form.embedding.device,
  }
  if (form.embedding.api_key.trim()) embedding.api_key = form.embedding.api_key
  const rerank: VectorSettingsInput["rerank"] = {
    backend: form.rerank.backend,
    model: form.rerank.model,
    endpoint: form.rerank.endpoint,
    device: form.rerank.device,
    max_candidates: form.rerank.max_candidates,
    max_length: form.rerank.max_length,
  }
  if (form.rerank.api_key.trim()) rerank.api_key = form.rerank.api_key
  return {
    schema_version: 1,
    backend: form.backend,
    embedded_path: form.embedded_path,
    fallback_to_embedded: form.fallback_to_embedded,
    timeout_seconds: form.timeout_seconds,
    milvus,
    embedding,
    rerank,
  }
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
  return <div className="settings-setting-row knowledge-setting-row">
    <div className="min-w-0">
      <label className="text-sm font-medium" htmlFor={id}>{label}</label>
      <p className="mt-1 text-xs text-muted-foreground">{help}</p>
    </div>
    <div className="settings-setting-control">{children}</div>
  </div>
}

function SelectField({
  id,
  value,
  onChange,
  options,
}: {
  id: string
  value: string
  onChange: (value: string) => void
  options: Array<{ value: string; label: string }>
}) {
  return <select
    className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm shadow-sm outline-none focus:ring-1 focus:ring-ring"
    id={id}
    onChange={(event) => onChange(event.target.value)}
    value={value}
  >
    {options.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
  </select>
}

function TestResult({ result }: { result: VectorSettingsTestResponse | null }) {
  if (!result) return null
  return <div className={`flex items-center gap-2 text-sm ${result.ok ? "text-[#2f9e6e]" : "text-destructive"}`} role="status">
    {result.ok ? <CheckCircle2 className="size-4" /> : <XCircle className="size-4" />}
    <span>{result.message}</span>
  </div>
}

export default function ExternalLinksSettings() {
  const location = useLocation()
  const activeSection = location.pathname.endsWith("/rerank") ? "rerank" : "vector"
  const [response, setResponse] = useState<VectorSettingsResponse | null>(null)
  const [form, setForm] = useState<FormState | null>(null)
  const [busy, setBusy] = useState<"save" | "test" | null>(null)
  const [error, setError] = useState("")
  const [message, setMessage] = useState("")
  const [testResult, setTestResult] = useState<VectorSettingsTestResponse | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    loadVectorSettings(controller.signal).then((value) => {
      setResponse(value)
      setForm(formFromResponse(value))
    }).catch((reason) => {
      if ((reason as Error).name !== "AbortError") setError((reason as Error).message)
    })
    return () => controller.abort()
  }, [])

  function patchForm(patch: Partial<FormState>) {
    setForm((current) => current ? { ...current, ...patch } : current)
  }

  function patchMilvus(patch: Partial<FormState["milvus"]>) {
    setForm((current) => current ? { ...current, milvus: { ...current.milvus, ...patch } } : current)
  }

  function patchEmbedding(patch: Partial<FormState["embedding"]>) {
    setForm((current) => current ? { ...current, embedding: { ...current.embedding, ...patch } } : current)
  }

  function patchRerank(patch: Partial<FormState["rerank"]>) {
    setForm((current) => current ? { ...current, rerank: { ...current.rerank, ...patch } } : current)
  }

  async function save() {
    if (!form || busy) return
    setBusy("save"); setError(""); setMessage(""); setTestResult(null)
    try {
      const value = await saveVectorSettings(payloadFromForm(form))
      setResponse(value)
      setForm(formFromResponse(value))
      setMessage("外部链接设置已保存")
    } catch (reason) {
      setError((reason as Error).message)
    } finally {
      setBusy(null)
    }
  }

  async function testConnection() {
    if (!form || busy) return
    setBusy("test"); setError(""); setMessage("")
    try {
      setTestResult(await testVectorSettings(payloadFromForm(form)))
    } catch (reason) {
      setError((reason as Error).message)
      setTestResult(null)
    } finally {
      setBusy(null)
    }
  }

  return <SettingsPageLayout
    group="knowledge"
  >
    {error ? <Alert className="mb-5" variant="destructive"><AlertDescription>{error}</AlertDescription></Alert> : null}
    {!form || !response ? <p className="text-sm text-muted-foreground">正在读取设置</p> : <div className="space-y-5">
      <Card className={activeSection === "vector" ? "settings-card" : "settings-card hidden"}>
        <CardHeader>
          <CardTitle className="flex items-center gap-2"><Link2 className="size-5" />向量存储</CardTitle>
          <CardDescription>Milvus 只负责保存和检索向量；文字转向量的 Embedding 配置见下方。</CardDescription>
        </CardHeader>
        <CardContent className="settings-card-content">
          <Field id="vector-backend" label="向量数据库后端" help="内置：不依赖外部服务；Milvus：使用外部服务；自动：优先 Milvus，失败时回退内置。">
            <SelectField id="vector-backend" onChange={(backend) => patchForm({ backend: backend as VectorStoreBackend })} options={[
              { value: "embedded", label: "中台内置（推荐起步）" },
              { value: "auto", label: "自动：Milvus 优先，内置回退" },
              { value: "milvus", label: "外部 Milvus" },
            ]} value={form.backend} />
          </Field>
          <Field id="vector-timeout" label="请求超时（秒）" help="仅影响外部 Milvus 或外部 Embedding 请求，范围 1~120 秒。">
            <Input id="vector-timeout" max={120} min={1} onChange={(event) => patchForm({ timeout_seconds: Number(event.target.value) })} type="number" value={form.timeout_seconds} />
          </Field>
          <Field id="embedded-path" label="内置向量索引路径" help={`相对仓库根目录保存；当前 ${response.status.embedded_vector_available ? "已有向量索引" : "尚未生成向量索引"}。`}>
            <Input id="embedded-path" onChange={(event) => patchForm({ embedded_path: event.target.value })} value={form.embedded_path} />
          </Field>
          <div className="settings-setting-row">
            <div><p className="text-sm font-medium">外部不可用时回退内置</p><p className="mt-1 text-xs text-muted-foreground">只对 auto 模式生效；回退状态会记录在检索元数据中。</p></div>
            <Switch aria-label="外部不可用时回退内置" checked={form.fallback_to_embedded} onCheckedChange={(fallback_to_embedded) => patchForm({ fallback_to_embedded })} />
          </div>
        </CardContent>
      </Card>

      <Card className={activeSection === "rerank" ? "settings-card" : "settings-card hidden"}>
        <CardHeader>
          <CardTitle>Cross-Encoder 重排</CardTitle>
          <CardDescription>对 FTS、拓扑和向量召回出的候选分段做二次相关性判断。默认关闭，不改变现有检索结果。</CardDescription>
        </CardHeader>
        <CardContent className="settings-card-content">
          <Field id="rerank-backend" label="Rerank 后端" help="关闭时不加载重排模型；local 使用 sentence-transformers CrossEncoder；external 调用兼容 POST /rerank 的服务。">
            <SelectField id="rerank-backend" onChange={(backend) => patchRerank({ backend: backend as RerankBackend, model: backend === "local" && !form.rerank.model ? DEFAULT_LOCAL_RERANK_MODEL : form.rerank.model })} options={[
              { value: "disabled", label: "禁用重排（默认）" },
              { value: "local", label: "本地 Cross-Encoder" },
              { value: "external", label: "外部 Rerank API" },
            ]} value={form.rerank.backend} />
          </Field>
          <Field id="rerank-model" label="重排模型名称或路径" help="本地可填已下载的模型目录；可使用 BAAI/bge-reranker-v2-m3 等 Cross-Encoder 模型。启用后首次推理可能加载模型。">
            <Input id="rerank-model" onChange={(event) => patchRerank({ model: event.target.value })} placeholder="例如 BAAI/bge-reranker-v2-m3" value={form.rerank.model} />
          </Field>
          <Field id="rerank-endpoint" label="Rerank API 地址" help="仅 external 需要；可填写服务根地址或完整的 /rerank 地址，不要把密钥写进 URL。">
            <Input disabled={form.rerank.backend !== "external"} id="rerank-endpoint" onChange={(event) => patchRerank({ endpoint: event.target.value })} placeholder="http://127.0.0.1:8000" value={form.rerank.endpoint} />
          </Field>
          <Field id="rerank-api-key" label="API Key（可选）" help={response.settings.rerank.api_key_configured ? "当前已配置 API Key；留空保持不变，只保存不回显。" : "无认证的内网服务可以留空。"}>
            <Input autoComplete="new-password" id="rerank-api-key" onChange={(event) => patchRerank({ api_key: event.target.value })} placeholder={response.settings.rerank.api_key_configured ? "留空保持已保存 key" : "可选"} type="password" value={form.rerank.api_key} />
          </Field>
          <Field id="rerank-max-candidates" label="最多重排候选数" help="只对前 N 个已通过硬过滤的候选调用模型；范围 5~100，越大越慢。">
            <Input id="rerank-max-candidates" max={100} min={5} onChange={(event) => patchRerank({ max_candidates: Number(event.target.value) })} type="number" value={form.rerank.max_candidates} />
          </Field>
          <Field id="rerank-max-length" label="单次最大输入长度" help="Cross-Encoder tokenizer 的最大 token 长度；范围 64~4096，建议先用 512。">
            <Input id="rerank-max-length" max={4096} min={64} onChange={(event) => patchRerank({ max_length: Number(event.target.value) })} type="number" value={form.rerank.max_length} />
          </Field>
          <Field id="rerank-device" label="本地设备" help="local 默认 cpu；GPU 是否可用取决于安装的 PyTorch 运行时。">
            <Input disabled={form.rerank.backend !== "local"} id="rerank-device" onChange={(event) => patchRerank({ device: event.target.value })} value={form.rerank.device} />
          </Field>
          <div className="settings-setting-info-row">
            <p className="font-medium">执行位置</p>
            <p className="mt-1 text-xs text-muted-foreground">仅 investigation、artifact 以及标准告警失败后的 investigation fallback 会调用；qa、alert 初始路由和 default 不调用。模型失败会自动回退到原有排序，并在 ContextPack retrieval_meta 中记录。</p>
          </div>
        </CardContent>
      </Card>

      <Card className={activeSection === "vector" ? "settings-card" : "settings-card hidden"}>
        <CardHeader><CardTitle>Milvus 连接</CardTitle><CardDescription>只有选择 Milvus 或自动模式时才需要填写。这里填写 Milvus 的 HTTP REST 地址，通常是 9091 端口，不是原生 gRPC 的 19530 端口。</CardDescription></CardHeader>
        <CardContent className="settings-card-content">
          <Field id="milvus-endpoint" label="Milvus REST 地址" help="例如 http://127.0.0.1:9091；不要把 token 或 query 参数写进 URL。">
            <Input id="milvus-endpoint" onChange={(event) => patchMilvus({ endpoint: event.target.value })} placeholder="http://127.0.0.1:9091" value={form.milvus.endpoint} />
          </Field>
          <Field id="milvus-collection" label="Collection 名称" help="向量记录所在的 collection，例如 ops_agent_chunks。">
            <Input id="milvus-collection" onChange={(event) => patchMilvus({ collection: event.target.value })} value={form.milvus.collection} />
          </Field>
          <Field id="milvus-vector-field" label="向量字段名" help="Collection 中的向量字段，默认是 vector；必须与 Milvus schema 一致。">
            <Input id="milvus-vector-field" onChange={(event) => patchMilvus({ vector_field: event.target.value })} value={form.milvus.vector_field} />
          </Field>
          <Field id="milvus-token" label="Token（可选）" help={response.settings.milvus.token_configured ? "当前已配置 token；留空表示保持不变，输入新值会替换。" : "无认证的本地 Milvus 可以留空。token 只保存，不会回显。"}>
            <Input autoComplete="new-password" id="milvus-token" onChange={(event) => patchMilvus({ token: event.target.value })} placeholder={response.settings.milvus.token_configured ? "留空保持已保存 token" : "可选"} type="password" value={form.milvus.token} />
          </Field>
        </CardContent>
      </Card>

      <Card className={activeSection === "vector" ? "settings-card" : "settings-card hidden"}>
        <CardHeader><CardTitle>Embedding 模型</CardTitle><CardDescription>Embedding 模型把查询和文档章节转换为向量。禁用时仍可使用 FTS5 + 拓扑检索。</CardDescription></CardHeader>
        <CardContent className="settings-card-content">
          <Field id="embedding-backend" label="Embedding 后端" help="本地模型需要安装对应运行时；外部模型需要 OpenAI-compatible /embeddings 接口。">
            <SelectField id="embedding-backend" onChange={(backend) => patchEmbedding({ backend: backend as EmbeddingBackend, model: backend === "local" && !form.embedding.model ? DEFAULT_LOCAL_EMBEDDING_MODEL : form.embedding.model })} options={[
              { value: "disabled", label: "禁用语义向量" },
              { value: "local", label: "本地 sentence-transformers" },
              { value: "external", label: "外部 Embedding API" },
            ]} value={form.embedding.backend} />
          </Field>
          <Field id="embedding-model" label="模型名称或路径" help="本地填写模型目录或模型 ID；外部填写服务支持的模型名。">
            <Input id="embedding-model" onChange={(event) => patchEmbedding({ model: event.target.value })} placeholder="例如 multilingual embedding model" value={form.embedding.model} />
          </Field>
          <Field id="embedding-endpoint" label="Embedding API 地址" help="仅 external 需要，例如 http://127.0.0.1:8000/v1；不要与 Milvus REST 地址混用。">
            <Input disabled={form.embedding.backend !== "external"} id="embedding-endpoint" onChange={(event) => patchEmbedding({ endpoint: event.target.value })} placeholder="http://127.0.0.1:8000/v1" value={form.embedding.endpoint} />
          </Field>
          <Field id="embedding-api-key" label="API Key（可选）" help={response.settings.embedding.api_key_configured ? "当前已配置 API Key；留空保持不变。" : "无认证的内网服务可以留空；只保存，不回显。"}>
            <Input autoComplete="new-password" id="embedding-api-key" onChange={(event) => patchEmbedding({ api_key: event.target.value })} placeholder={response.settings.embedding.api_key_configured ? "留空保持已保存 key" : "可选"} type="password" value={form.embedding.api_key} />
          </Field>
          <Field id="embedding-dimensions" label="向量维度（可选）" help="必须和模型/向量库 schema 一致；不确定时留空，由服务返回维度。">
            <Input id="embedding-dimensions" min={1} onChange={(event) => patchEmbedding({ dimensions: event.target.value ? Number(event.target.value) : null })} placeholder="例如 384 或 1024" type="number" value={form.embedding.dimensions ?? ""} />
          </Field>
          <Field id="embedding-device" label="本地设备" help="本地模型默认 cpu；GPU 支持取决于安装的运行时。">
            <Input disabled={form.embedding.backend !== "local"} id="embedding-device" onChange={(event) => patchEmbedding({ device: event.target.value })} value={form.embedding.device} />
          </Field>
        </CardContent>
      </Card>

      <Card className={activeSection === "vector" ? "settings-card" : "settings-card hidden"}>
        <CardHeader><CardTitle>当前状态</CardTitle><CardDescription>状态读取不会主动连接 Milvus；点击“测试连接”才会发起外部请求。</CardDescription></CardHeader>
        <CardContent className="settings-status-grid text-sm">
          <div className="rounded-md border px-3 py-2"><span className="text-muted-foreground">检索快照：</span><Badge variant={response.status.retrieval_index_available ? "default" : "secondary"}>{response.status.retrieval_index_available ? "可用" : "缺失"}</Badge></div>
          <div className="rounded-md border px-3 py-2"><span className="text-muted-foreground">内置向量：</span><Badge variant={response.status.embedded_vector_available ? "default" : "secondary"}>{response.status.embedded_vector_available ? "可用" : "未生成"}</Badge></div>
          <div className="rounded-md border px-3 py-2"><span className="text-muted-foreground">向量数量：</span><span className="font-medium">{response.status.embedded_vector_count}</span>{response.status.embedded_vector_dimension ? <span className="ml-1 text-xs text-muted-foreground">({response.status.embedded_vector_dimension} 维)</span> : null}</div>
          <div className="rounded-md border px-3 py-2"><span className="text-muted-foreground">模型运行时：</span><Badge variant={response.status.local_embedding_runtime_available ? "default" : "secondary"}>{response.status.local_embedding_runtime_available ? "已安装" : "未安装"}</Badge></div>
          <div className="rounded-md border px-3 py-2"><span className="text-muted-foreground">语义检索：</span><Badge variant={response.status.semantic_ready ? "default" : "secondary"}>{response.status.semantic_ready ? "已就绪" : "未就绪"}</Badge></div>
          <div className="rounded-md border px-3 py-2"><span className="text-muted-foreground">重排运行时：</span><Badge variant={response.status.local_rerank_runtime_available ? "default" : "secondary"}>{response.status.local_rerank_runtime_available ? "已安装" : "未安装"}</Badge></div>
          <div className="rounded-md border px-3 py-2"><span className="text-muted-foreground">重排状态：</span><Badge variant={response.status.rerank_ready ? "default" : "secondary"}>{form.rerank.backend === "disabled" ? "已关闭" : response.status.rerank_ready ? "已就绪" : "未就绪"}</Badge></div>
        </CardContent>
      </Card>

      <div className="settings-page-bottom-actions">
        <Button disabled={busy !== null} onClick={() => void save()}><Save />{busy === "save" ? "保存中" : "保存设置"}</Button>
        <Button disabled={busy !== null} onClick={() => void testConnection()} variant="outline"><TestTube2 />{busy === "test" ? "测试中" : "测试连接"}</Button>
        {message ? <span className="text-sm text-[#2f9e6e]">{message}</span> : null}
        <TestResult result={testResult} />
      </div>
      <p className="text-xs text-muted-foreground">配置文件：{response.status.config_file} · 当前本地模型：{activeSection === "vector" ? response.status.embedding_model || "未配置" : form.rerank.model || "未配置"} · token/API Key 只显示是否已配置，不会返回明文。</p>
      {activeSection === "vector" && form.embedding.backend === "local" && !response.status.local_embedding_runtime_available ? <p className="text-xs text-muted-foreground">首次使用本地 BGE 前执行：<code>.venv/bin/pip install -r requirements-vector.txt</code>，然后执行 <code>.venv/bin/python scripts/prepare_bge_model.py</code>。</p> : null}
      {activeSection === "rerank" && form.rerank.backend === "local" && !response.status.local_rerank_runtime_available ? <p className="text-xs text-muted-foreground">本地 Cross-Encoder 与 Embedding 共用 sentence-transformers 运行时；安装依赖后，再将模型下载或填写本地模型目录。</p> : null}
    </div>}
  </SettingsPageLayout>
}
