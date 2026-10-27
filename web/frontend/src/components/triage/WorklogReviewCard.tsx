import { useState } from "react"
import { CheckCircle2, NotebookPen, XCircle } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import type { ApprovalDecision, KbReviewOutcome, WorklogReviewRequest } from "@/types"


type WorklogReviewCardProps = {
  request: WorklogReviewRequest
  outcome: KbReviewOutcome | null
  manual: boolean
  busy: boolean
  onDecision: (
    approvalId: string,
    decision: ApprovalDecision,
    feedback?: string,
  ) => Promise<void>
}


export function WorklogReviewCard({
  request,
  outcome,
  manual,
  busy,
  onDecision,
}: WorklogReviewCardProps) {
  const [feedback, setFeedback] = useState("")
  return (
    <section className="triage-approval" aria-label="worklog 落档确认">
      <div className="flex items-start gap-3">
        <NotebookPen className="mt-0.5 size-5 shrink-0 text-[#b7791f]" aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <strong className="text-sm">确认本次 worklog 预览</strong>
          <pre className="mt-3 max-h-60 overflow-auto whitespace-pre-wrap break-words bg-[var(--pre-bg)] p-3 font-mono text-[11px] leading-5 text-[var(--pre-fg)]">
            {request.preview}
          </pre>

          {outcome ? (
            <p className={`mt-4 flex items-center gap-2 text-sm ${
              outcome.decision === "approve" ? "text-[#247a56]" : "text-muted-foreground"
            }`}>
              {outcome.decision === "approve"
                ? <CheckCircle2 className="size-4" />
                : <XCircle className="size-4" />}
              <span>
                {outcome.decision === "approve"
                  ? "已确认，条目写入当日 worklog"
                  : outcome.decision === "revise"
                    ? "已提交修改意见，正在重新生成预览"
                    : "已取消本次写入"}
              </span>
            </p>
          ) : !manual ? (
            <p className="mt-4 text-xs text-muted-foreground">
              自动任务的 worklog 不在中台确认。
            </p>
          ) : (
            <div className="mt-4 border-t pt-4">
              <Textarea
                className="mb-3 min-h-20"
                onChange={(event) => setFeedback(event.target.value)}
                placeholder="需要调整时填写修改意见"
                value={feedback}
              />
              <div className="flex flex-wrap justify-end gap-2">
              <Button
                disabled={busy}
                onClick={() => void onDecision(request.approvalId, "reject")}
                size="sm"
                variant="outline"
              >
                跳过
              </Button>
              <Button
                disabled={busy || !feedback.trim()}
                onClick={() => void onDecision(request.approvalId, "revise", feedback.trim())}
                size="sm"
                variant="outline"
              >
                按意见重生成
              </Button>
              <Button
                disabled={busy}
                onClick={() => void onDecision(request.approvalId, "approve")}
                size="sm"
              >
                确认记录
              </Button>
              </div>
            </div>
          )}
        </div>
      </div>
    </section>
  )
}
