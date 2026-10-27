import { useCallback, useEffect, useState } from "react"
import { Link2, LoaderCircle, Plus, Unlink } from "lucide-react"
import { Link } from "react-router-dom"
import { Button } from "@/components/ui/button"
import { WorkItemPickerDialog, WorkItemStatusBadge } from "@/components/workitems/WorkItemBits"
import { createWorkItem, linkWorkItem, unlinkWorkItem, workItemByLink } from "@/lib/workItemsApi"
import type { WorkItemSummary } from "@/lib/workItemsTypes"
import type { TriageTask } from "@/types"

/** 任务所属工作项:显示、挂到已有项、用任务标题新建,或解除。挂载只由人操作,Agent 不碰。 */
export function TaskWorkItem({ task }: { task: TriageTask }) {
  const [item, setItem] = useState<WorkItemSummary | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [pickerOpen, setPickerOpen] = useState(false)
  const [error, setError] = useState("")

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true)
    try {
      setItem((await workItemByLink("task", task.task_id, signal)).item)
      setError("")
    } catch (reason) {
      if ((reason as Error).name !== "AbortError") setError((reason as Error).message)
    } finally {
      setLoading(false)
    }
  }, [task.task_id])

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => controller.abort()
  }, [load])

  async function run(action: () => Promise<unknown>) {
    setBusy(true)
    setError("")
    try {
      await action()
      await load()
    } catch (reason) {
      setError((reason as Error).message)
    } finally {
      setBusy(false)
    }
  }

  if (loading) return <p className="mt-3 flex items-center gap-2 text-sm text-muted-foreground"><LoaderCircle className="size-4 animate-spin" />正在读取</p>
  return <div className="mt-3 space-y-2">
    {item ? <div className="flex items-start gap-2">
      <WorkItemStatusBadge status={item.status} />
      <Link className="min-w-0 flex-1 break-words text-sm hover:underline" to={`/work-items/${item.id}`}>{item.title}</Link>
      <Button aria-label="解除工作项挂载" className="size-7" disabled={busy} onClick={() => void run(() => unlinkWorkItem(item.id, "task", task.task_id))} size="icon" title="解除挂载" variant="ghost"><Unlink /></Button>
    </div> : <>
      <p className="text-sm text-muted-foreground">未挂到工作项</p>
      <div className="flex flex-wrap gap-2">
        <Button disabled={busy} onClick={() => setPickerOpen(true)} size="sm" variant="outline"><Link2 data-icon="inline-start" />挂到已有</Button>
        <Button disabled={busy} onClick={() => void run(async () => {
          const created = await createWorkItem({ title: (task.display_title || task.initial_message).slice(0, 200), status: "in_progress" })
          await linkWorkItem(created.item.id, "task", task.task_id)
        })} size="sm" variant="outline">{busy ? <LoaderCircle className="animate-spin" data-icon="inline-start" /> : <Plus data-icon="inline-start" />}新建并挂上</Button>
      </div>
    </>}
    {error ? <p className="text-xs text-destructive" role="alert">{error}</p> : null}
    <WorkItemPickerDialog
      description="任务的报告、产物、审批与写操作会显示在该工作项下，任务产生的工作日志也自动归过去。"
      onOpenChange={setPickerOpen}
      onPick={async (picked) => { await linkWorkItem(picked.id, "task", task.task_id); await load() }}
      open={pickerOpen}
      title="挂到工作项"
    />
  </div>
}
