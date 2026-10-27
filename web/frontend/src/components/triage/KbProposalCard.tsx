import { BookOpen } from "lucide-react"
import { ApprovalActions, type ApprovalActionsText } from "@/components/triage/ApprovalActions"
import type { ApprovalDecision, KbProposalRequest, KbReviewOutcome } from "@/types"


const MODE_LABEL: Record<string, string> = {
  new: "新建知识文档",
  append_case: "修改现有文档（追加案例）",
}

const TEXT: ApprovalActionsText = {
  outcome: {
    approve: "已同意，Agent 接着给出文档预览供审核",
    revise: "已要求调整，Agent 会修改请求后重新提出",
    reject: "已拒绝，本次排查不沉淀知识库",
  },
  autoTask: "自动任务的知识请求不在中台确认。",
  feedbackLabel: "落档请求调整意见",
  feedbackPlaceholder: "说明这次落档请求要怎么调整（如改目标文档、改标题方向）",
  submitFeedback: "提交调整意见",
  reject: "拒绝落档",
  revise: "要求调整",
  approve: "同意，给出预览",
}


type KbProposalCardProps = {
  request: KbProposalRequest
  outcome: KbReviewOutcome | null
  manual: boolean
  busy: boolean
  onDecision: (
    approvalId: string,
    decision: ApprovalDecision,
    feedback?: string,
  ) => Promise<void>
}


export function KbProposalCard({
  request,
  outcome,
  manual,
  busy,
  onDecision,
}: KbProposalCardProps) {
  return (
    <section className="triage-approval" aria-label="知识库落档请求">
      <div className="flex items-start gap-3">
        <BookOpen className="mt-0.5 size-5 shrink-0 text-[#b7791f]" aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <strong className="text-sm">
            Agent 请求{MODE_LABEL[request.mode] || request.mode}
          </strong>
          <p className="mt-1 break-all font-mono text-[11px] text-muted-foreground">
            {request.doc}
          </p>
          <dl className="triage-approval-facts">
            <div><dt>标题</dt><dd>{request.title || "未提供"}</dd></div>
            {request.symptoms.length ? (
              <div>
                <dt>症状</dt>
                <dd>{request.symptoms.join("；")}</dd>
              </div>
            ) : null}
            {request.summary ? (
              <div><dt>机理</dt><dd>{request.summary}</dd></div>
            ) : null}
          </dl>
          <p className="mt-2 text-xs text-muted-foreground">
            同意后 Agent 会渲染完整文档预览，再次确认后才会写入知识库。
          </p>

          <ApprovalActions
            approvalId={request.approvalId}
            busy={busy}
            manual={manual}
            onDecision={onDecision}
            outcome={outcome}
            text={TEXT}
          />
        </div>
      </div>
    </section>
  )
}
