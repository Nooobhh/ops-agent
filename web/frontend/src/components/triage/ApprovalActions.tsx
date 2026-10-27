import { useState } from "react"
import { CheckCircle2, PencilLine, XCircle } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import type { ApprovalDecision, KbReviewOutcome } from "@/types"


export type ApprovalActionsText = {
  outcome: Record<ApprovalDecision, string>
  autoTask: string
  feedbackLabel: string
  feedbackPlaceholder: string
  submitFeedback: string
  reject: string
  revise: string
  approve: string
}


/** 人工审批卡共用的裁决区:已裁决结果 / 自动任务说明 / 打回意见输入 / 三个裁决按钮。 */
export function ApprovalActions({
  approvalId,
  outcome,
  manual,
  busy,
  onDecision,
  text,
}: {
  approvalId: string
  outcome: KbReviewOutcome | null
  manual: boolean
  busy: boolean
  onDecision: (
    approvalId: string,
    decision: ApprovalDecision,
    feedback?: string,
  ) => Promise<void>
  text: ApprovalActionsText
}) {
  const [revising, setRevising] = useState(false)
  const [feedback, setFeedback] = useState("")

  if (outcome) {
    return (
      <p className={`mt-4 flex items-center gap-2 text-sm ${
        outcome.decision === "approve" ? "text-[#247a56]" : "text-muted-foreground"
      }`}>
        {outcome.decision === "approve"
          ? <CheckCircle2 className="size-4" />
          : outcome.decision === "revise"
            ? <PencilLine className="size-4" />
            : <XCircle className="size-4" />}
        <span className="min-w-0">
          {text.outcome[outcome.decision]}
          {outcome.feedback ? `：${outcome.feedback}` : ""}
        </span>
      </p>
    )
  }
  if (!manual) {
    return <p className="mt-4 text-xs text-muted-foreground">{text.autoTask}</p>
  }
  if (revising) {
    return (
      <div className="mt-4 border-t pt-4">
        <Textarea
          aria-label={text.feedbackLabel}
          autoFocus
          className="max-h-40 min-h-20"
          disabled={busy}
          maxLength={2000}
          onChange={(event) => setFeedback(event.target.value)}
          placeholder={text.feedbackPlaceholder}
          value={feedback}
        />
        <div className="mt-3 flex justify-end gap-2">
          <Button
            disabled={busy}
            onClick={() => { setRevising(false); setFeedback("") }}
            size="sm"
            variant="outline"
          >
            返回
          </Button>
          <Button
            disabled={busy || !feedback.trim()}
            onClick={() => void onDecision(approvalId, "revise", feedback.trim())}
            size="sm"
          >
            {text.submitFeedback}
          </Button>
        </div>
      </div>
    )
  }
  return (
    <div className="mt-4 flex flex-wrap justify-end gap-2 border-t pt-4">
      <Button
        disabled={busy}
        onClick={() => void onDecision(approvalId, "reject")}
        size="sm"
        variant="outline"
      >
        {text.reject}
      </Button>
      <Button
        disabled={busy}
        onClick={() => setRevising(true)}
        size="sm"
        variant="outline"
      >
        {text.revise}
      </Button>
      <Button
        disabled={busy}
        onClick={() => void onDecision(approvalId, "approve")}
        size="sm"
      >
        {text.approve}
      </Button>
    </div>
  )
}
