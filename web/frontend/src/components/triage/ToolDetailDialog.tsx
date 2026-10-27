import { type ReactNode, useEffect, useState } from "react"
import { LoaderCircle } from "lucide-react"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { MarkdownView } from "@/components/MarkdownView"
import { loadTriageKbDoc, loadTriageReport } from "@/lib/triageApi"
import type { ToolBlock } from "@/lib/triageEvents"


const DOCUMENT_TITLE = {
  report: "执行报告",
  kb: "知识库草稿",
} as const


export function ToolDetailDialog({
  activity,
  taskId,
  children,
}: {
  activity: ToolBlock
  taskId: string
  children: ReactNode
}) {
  const [open, setOpen] = useState(false)
  const document = useToolDocument(activity, taskId, open)

  return (
    <Dialog onOpenChange={setOpen} open={open}>
      <DialogTrigger asChild>{children}</DialogTrigger>
      <DialogContent className="min-w-0 max-h-[calc(100dvh-2rem)] max-w-2xl overflow-x-hidden overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="break-all font-mono text-base">
            {activity.label || "工具活动"}
          </DialogTitle>
          <DialogDescription>
            {activity.scope || "未指定范围"}
            {activity.outcome ? ` · ${activity.outcome}` : ""}
            {activity.durationSeconds !== null
              ? ` · ${activity.durationSeconds.toFixed(1)} 秒`
              : ""}
          </DialogDescription>
        </DialogHeader>
        <div className="grid min-w-0 gap-4">
          {activity.outcome ? (
            <section className="grid gap-2 rounded border p-3 text-xs">
              <div className="grid grid-cols-[96px_minmax(0,1fr)] gap-2">
                <span className="text-muted-foreground">结果状态</span>
                <span>{activity.outcome}</span>
              </div>
              {activity.executionState ? (
                <div className="grid grid-cols-[96px_minmax(0,1fr)] gap-2">
                  <span className="text-muted-foreground">执行状态</span>
                  <span>{activity.executionState === "executed" ? "已执行" : "未执行"}</span>
                </div>
              ) : null}
              {activity.commandStatus && activity.commandStatus !== "not_applicable" ? (
                <div className="grid grid-cols-[96px_minmax(0,1fr)] gap-2">
                  <span className="text-muted-foreground">命令退出</span>
                  <span>{activity.commandStatus}</span>
                </div>
              ) : null}
              {activity.skipReason ? (
                <div className="grid grid-cols-[96px_minmax(0,1fr)] gap-2">
                  <span className="text-muted-foreground">未执行原因</span>
                  <span>{activity.skipReason}</span>
                </div>
              ) : null}
              {activity.timing ? (
                <div className="grid grid-cols-[96px_minmax(0,1fr)] gap-2">
                  <span className="text-muted-foreground">阶段耗时</span>
                  <span className="break-words">
                    总计 {activity.timing.total_ms}ms · 准入 {activity.timing.admission_ms}ms · 调度 {activity.timing.dispatch_ms}ms · 远端 {activity.timing.remote_exec_ms}ms
                  </span>
                </div>
              ) : null}
              {activity.targetKind ? (
                <div className="grid grid-cols-[96px_minmax(0,1fr)] gap-2">
                  <span className="text-muted-foreground">目标类型</span>
                  <span>{activity.targetKind}</span>
                </div>
              ) : null}
              {activity.connectionStatus ? (
                <div className="grid grid-cols-[96px_minmax(0,1fr)] gap-2">
                  <span className="text-muted-foreground">连接状态</span>
                  <span>{activity.connectionStatus}</span>
                </div>
              ) : null}
              {activity.evidenceEligible !== undefined ? (
                <div className="grid grid-cols-[96px_minmax(0,1fr)] gap-2">
                  <span className="text-muted-foreground">证据资格</span>
                  <span>{activity.evidenceEligible ? "可作为实时证据" : "不可作为实时证据"}</span>
                </div>
              ) : null}
              {activity.errorDisposition ? (
                <div className="grid grid-cols-[96px_minmax(0,1fr)] gap-2">
                  <span className="text-muted-foreground">失败处置</span>
                  <span>{activity.errorDisposition}</span>
                </div>
              ) : null}
            </section>
          ) : null}
          {activity.commands.length ? (
            <section className="min-w-0">
              <SectionTitle>服务器执行命令</SectionTitle>
              <ul className="triage-commands">
                {activity.commands.map((command, index) => (
                  <li key={`${activity.eventId}-${index}`}>
                    <code>{command}</code>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}

          {activity.documentKind ? (
            <section className="min-w-0">
              <SectionTitle>
                {DOCUMENT_TITLE[activity.documentKind]}
              </SectionTitle>
              {document.loading ? (
                <p className="flex items-center gap-2 text-sm text-muted-foreground">
                  <LoaderCircle className="size-4 animate-spin" />
                  正在读取文档
                </p>
              ) : document.error ? (
                <p className="text-sm text-destructive">{document.error}</p>
              ) : (
                <MarkdownView content={document.content} />
              )}
            </section>
          ) : (
            <>
              <DetailSection title="调用参数" value={activity.argumentsText} />
              <DetailSection title="执行结果" value={activity.resultText} />
            </>
          )}

          {activity.documentKind ? (
            <details className="min-w-0">
              <summary className="cursor-pointer text-xs font-semibold text-muted-foreground">
                原始调用记录
              </summary>
              <div className="mt-3 grid gap-4">
                <DetailSection title="调用参数" value={activity.argumentsText} />
                <DetailSection title="执行结果" value={activity.resultText} />
              </div>
            </details>
          ) : null}
        </div>
      </DialogContent>
    </Dialog>
  )
}


type DocumentState = {loading: boolean; error: string; content: string}


function useToolDocument(
  activity: ToolBlock,
  taskId: string,
  open: boolean,
): DocumentState {
  const [state, setState] = useState<DocumentState>({
    loading: false,
    error: "",
    content: "",
  })
  const kind = activity.documentKind

  useEffect(() => {
    if (!open || !kind) return
    const controller = new AbortController()
    setState({loading: true, error: "", content: ""})
    const load = kind === "report" ? loadTriageReport : loadTriageKbDoc
    load(taskId, controller.signal)
      .then((content) => setState({loading: false, error: "", content}))
      .catch((reason: Error) => {
        if (reason.name === "AbortError") return
        setState({loading: false, error: reason.message, content: ""})
      })
    return () => controller.abort()
  }, [kind, open, taskId])

  return state
}


function SectionTitle({children}: {children: ReactNode}) {
  return (
    <h3 className="mb-2 text-xs font-semibold text-muted-foreground">
      {children}
    </h3>
  )
}


function DetailSection({title, value}: {title: string; value: string}) {
  return (
    <section className="min-w-0 max-w-full">
      <SectionTitle>{title}</SectionTitle>
      <pre className="min-w-0 max-w-full max-h-80 overflow-x-hidden overflow-y-auto whitespace-pre-wrap break-all bg-[var(--pre-bg)] p-3 font-mono text-xs leading-5 text-[var(--pre-fg)] [overflow-wrap:anywhere]">
        {value || "{}"}
      </pre>
    </section>
  )
}
