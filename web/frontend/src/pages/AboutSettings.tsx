import { useEffect, useState, type ReactNode } from "react"
import { SettingsPageLayout } from "@/components/SettingsLayout"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { fetchJson } from "@/lib/api"
import type { EnvInfo } from "@/types"

const ORIGIN_LABELS: Record<EnvInfo["origin"], string> = {
  example: "程序自带的示例环境（未挂载环境数据）",
  data_root: "数据目录下的 env/",
  configured: "paths.local.yaml 的 env_pack",
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="settings-setting-row">
      <div className="min-w-0 text-sm font-medium">{label}</div>
      <div className="settings-setting-control min-w-0 break-all text-sm">{children}</div>
    </div>
  )
}

export default function AboutSettings() {
  const [env, setEnv] = useState<EnvInfo | null>(null)
  const [error, setError] = useState("")

  useEffect(() => {
    const controller = new AbortController()
    fetchJson<EnvInfo>("/api/env", controller.signal)
      .then(setEnv)
      .catch((reason) => {
        if ((reason as Error).name !== "AbortError") setError((reason as Error).message)
      })
    return () => controller.abort()
  }, [])

  return (
    <SettingsPageLayout group="settings">
      {error ? <Alert className="mb-5" variant="destructive"><AlertDescription>{error}</AlertDescription></Alert> : null}
      <Card className="settings-card max-w-3xl">
        <CardHeader>
          <CardTitle>环境数据</CardTitle>
          <CardDescription>
            环境包（集群清单、拓扑、告警与资源映射、playbook、周报模板等）是本机的应用数据，不随程序发布。
            中台按序定位：paths.local.yaml 的 env_pack → 数据目录下的 env/ → 程序自带的示例环境；换了环境数据需重启中台。
          </CardDescription>
        </CardHeader>
        <CardContent className="settings-card-content">
          {env ? (
            <>
              <Row label="环境">{env.display_name}<span className="ml-2 text-muted-foreground">{env.id}</span></Row>
              <Row label="来源">{ORIGIN_LABELS[env.origin] ?? env.origin}</Row>
              <Row label="路径"><code className="font-mono text-xs">{env.path}</code></Row>
              <Row label="提交号">{env.commit ? <code className="font-mono text-xs">{env.commit}</code> : <span className="text-muted-foreground">无（环境目录不是 git 仓）</span>}</Row>
              <Row label="时区">{env.timezone}</Row>
              <Row label="集群">{env.clusters.map((cluster) => `${cluster.display_name}（${cluster.key}）`).join("、") || "—"}</Row>
            </>
          ) : error ? null : <p className="py-4 text-sm text-muted-foreground">正在加载</p>}
          <p className="pt-2 text-xs text-muted-foreground">
            换机或迁移时要带走的数据（环境数据、知识库、记录、本机配置与设置）见程序目录下的 docs/migration.md。
          </p>
        </CardContent>
      </Card>
    </SettingsPageLayout>
  )
}
