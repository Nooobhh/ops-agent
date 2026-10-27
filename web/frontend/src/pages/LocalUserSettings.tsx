import { useEffect, useState } from "react"
import { Save } from "lucide-react"
import { SettingsPageLayout } from "@/components/SettingsLayout"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { isValidUserId, loadLocalUser, saveLocalUser } from "@/lib/localUserApi"

export default function LocalUserSettings() {
  const [userId, setUserId] = useState("")
  const [displayName, setDisplayName] = useState("")
  const [savedId, setSavedId] = useState("")
  const [savedName, setSavedName] = useState("")
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState("")
  const [message, setMessage] = useState("")

  useEffect(() => {
    const controller = new AbortController()
    loadLocalUser(controller.signal)
      .then((response) => {
        if (response.error) setError(response.error)
        const user = response.user
        setUserId(user?.id ?? "")
        setDisplayName(user?.display_name ?? "")
        setSavedId(user?.id ?? "")
        setSavedName(user?.display_name ?? "")
      })
      .catch((reason) => {
        if ((reason as Error).name !== "AbortError") setError((reason as Error).message)
      })
      .finally(() => setLoading(false))
    return () => controller.abort()
  }, [])

  async function save() {
    setSaving(true)
    setError("")
    setMessage("")
    try {
      const response = await saveLocalUser(userId.trim(), displayName.trim())
      setSavedId(response.user?.id ?? "")
      setSavedName(response.user?.display_name ?? "")
      setDisplayName(response.user?.display_name ?? "")
      setMessage("本机用户已保存，此后新写入的记录以此为作者")
      window.dispatchEvent(new Event("local-user-changed"))
    } catch (reason) {
      setError((reason as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const trimmedId = userId.trim()
  const idValid = isValidUserId(trimmedId)
  const dirty = trimmedId !== savedId || displayName.trim() !== savedName

  return (
    <SettingsPageLayout group="settings">
      {error ? <Alert className="mb-5" variant="destructive"><AlertDescription>{error}</AlertDescription></Alert> : null}
      <Card className="settings-card max-w-2xl">
        <CardHeader>
          <CardTitle>本机用户</CardTitle>
          <CardDescription>本机数据的所有者。工作日志、排查记录与新建任务以此为作者；未设置前写入的记录读取时也按此用户归属。</CardDescription>
        </CardHeader>
        <CardContent className="settings-card-content">
          <div className="settings-setting-row">
            <div className="min-w-0">
              <label className="text-sm font-medium" htmlFor="local-user-id">用户 ID</label>
              <p className="mt-1 text-xs text-muted-foreground">小写字母或数字开头，可含 _ . -，最长 64 位；写入记录后不宜再改。</p>
            </div>
            <div className="settings-setting-control">
              <Input disabled={loading || saving} id="local-user-id" onChange={(event) => setUserId(event.target.value)} placeholder="例如 alice" value={userId} />
              {trimmedId && !idValid ? <p className="mt-1 text-xs text-destructive">用户 ID 格式不合法</p> : null}
            </div>
          </div>
          <div className="settings-setting-row">
            <div className="min-w-0">
              <label className="text-sm font-medium" htmlFor="local-user-name">显示名</label>
              <p className="mt-1 text-xs text-muted-foreground">只用于界面展示，留空则显示用户 ID。</p>
            </div>
            <div className="settings-setting-control">
              <Input disabled={loading || saving} id="local-user-name" maxLength={64} onChange={(event) => setDisplayName(event.target.value)} value={displayName} />
            </div>
          </div>
          <div className="settings-action-row">
            <Button disabled={loading || saving || !dirty || !idValid} onClick={() => void save()}><Save data-icon="inline-start" />{saving ? "保存中" : "保存配置"}</Button>
            {dirty ? <span className="text-xs font-medium text-[#b7791f]">未保存</span> : null}
            {message ? <span className="text-sm text-[#2f9e6e]">{message}</span> : null}
          </div>
        </CardContent>
      </Card>
    </SettingsPageLayout>
  )
}
