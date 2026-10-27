import { useEffect, useState } from "react"
import { ArrowLeft, FileText, LoaderCircle } from "lucide-react"
import { Link, useParams } from "react-router-dom"
import { MarkdownView } from "@/components/MarkdownView"
import { PageHeader } from "@/components/PageHeader"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { loadTriageReport, loadTroubleshootReport } from "@/lib/triageApi"

export default function TriageDocument() {
  const { taskId, runId } = useParams()
  const [content, setContent] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!taskId && !runId) {
      setError("缺少 Ops 任务标识")
      return
    }
    const controller = new AbortController()
    setContent(null)
    setError(null);
    (taskId
      ? loadTriageReport(taskId, controller.signal)
      : loadTroubleshootReport(runId as string, controller.signal))
      .then(setContent)
      .catch((reason: Error) => {
        if (reason.name !== "AbortError") setError(reason.message)
      })
    return () => controller.abort()
  }, [runId, taskId])

  const title = "执行报告"
  const backHref = taskId ? `/triage?task=${encodeURIComponent(taskId)}` : "/knowledge"

  return (
    <div className="mx-auto max-w-5xl">
      <PageHeader
        meta={taskId ? `任务 ${taskId}` : runId ? `排查 ${runId}` : undefined}
        title={title}
        actions={(
          <Button asChild size="sm" variant="outline">
            <Link to={backHref}>
              <ArrowLeft data-icon="inline-start" />
              返回 Agent-Ops
            </Link>
          </Button>
        )}
      />

      {error ? (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      ) : content === null ? (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <LoaderCircle className="size-4 animate-spin" aria-hidden="true" />
          正在加载{title}
        </div>
      ) : (
        <article className="rounded-md border bg-background p-6 shadow-sm">
          <div className="mb-5 flex items-center gap-2 border-b pb-4 text-sm font-medium">
            <FileText className="size-4 text-[var(--link)]" aria-hidden="true" />
            {title}
          </div>
          <MarkdownView content={content} />
        </article>
      )}
    </div>
  )
}
