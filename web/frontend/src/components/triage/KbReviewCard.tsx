import { useState } from "react"
import { BookOpen } from "lucide-react"
import { Button } from "@/components/ui/button"
import { MarkdownView } from "@/components/MarkdownView"
import { ApprovalActions, type ApprovalActionsText } from "@/components/triage/ApprovalActions"
import type { ApprovalDecision, KbReviewOutcome, KbReviewRequest } from "@/types"


const MODE_LABEL: Record<string, string> = {
  new: "新建知识文档",
  append_case: "向现有文档追加案例",
}

const TEXT: ApprovalActionsText = {
  outcome: {
    approve: "已同意落档，草稿写入知识库",
    revise: "已要求修改，Agent 会按意见重写草稿",
    reject: "已拒绝落档，本次排查不沉淀知识库",
  },
  autoTask: "自动任务的知识草稿不在中台确认。",
  feedbackLabel: "草稿修改意见",
  feedbackPlaceholder: "说明草稿要改什么，Agent 会据此重写后再次请求确认",
  submitFeedback: "提交修改意见",
  reject: "拒绝落档",
  revise: "要求修改",
  approve: "同意落档",
}


type KbReviewCardProps = {
  request: KbReviewRequest
  outcome: KbReviewOutcome | null
  manual: boolean
  busy: boolean
  onDecision: (
    approvalId: string,
    decision: ApprovalDecision,
    feedback?: string,
  ) => Promise<void>
}


/** unified diff 行着色:+ 增 / - 删 / @@ 定位,其余为上下文。 */
function diffLineClass(line: string): string {
  if (line.startsWith("+++") || line.startsWith("---")) return "triage-diff-meta"
  if (line.startsWith("@@")) return "triage-diff-hunk"
  if (line.startsWith("+")) return "triage-diff-add"
  if (line.startsWith("-")) return "triage-diff-del"
  return ""
}


export function KbReviewCard({
  request,
  outcome,
  manual,
  busy,
  onDecision,
}: KbReviewCardProps) {
  // 改动既有文档时默认先看 diff:全文里那几行新增太容易被淹没
  const [view, setView] = useState<"diff" | "full">("diff")

  return (
    <section className="triage-approval" aria-label="知识库落档确认">
      <div className="flex items-start gap-3">
        <BookOpen className="mt-0.5 size-5 shrink-0 text-[#b7791f]" aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <strong className="text-sm">本次排查是否落档知识库？</strong>
          <p className="mt-1 break-all font-mono text-[11px] text-muted-foreground">
            {MODE_LABEL[request.mode] || request.mode} · {request.doc}
          </p>
          {request.evidenceIssues?.length ? (
            <div className="mt-3 border-l-2 border-[#d69e2e] bg-[#d69e2e]/10 px-3 py-2 text-xs">
              <p className="font-medium">证据校验提示（不阻止人工同意落档）</p>
              <ul className="mt-1 list-disc space-y-1 pl-4 text-muted-foreground">
                {request.evidenceIssues.map((issue) => <li key={issue}>{issue}</li>)}
              </ul>
            </div>
          ) : null}
          {request.diff ? (
            <div className="mt-3">
              <div className="mb-2 flex flex-wrap gap-2">
                <Button
                  onClick={() => setView("diff")}
                  size="sm"
                  variant={view === "diff" ? "default" : "outline"}
                >
                  本次改动
                </Button>
                <Button
                  onClick={() => setView("full")}
                  size="sm"
                  variant={view === "full" ? "default" : "outline"}
                >
                  完整文档
                </Button>
              </div>
              {view === "diff" ? (
                <pre className="triage-kb-diff">
                  {request.diff.split("\n").map((line, index) => (
                    <span className={diffLineClass(line)} key={index}>
                      {line || " "}{"\n"}
                    </span>
                  ))}
                </pre>
              ) : (
                <div className="max-h-96 overflow-y-auto border bg-background p-3">
                  <MarkdownView content={request.markdown} />
                </div>
              )}
            </div>
          ) : (
            <div className="mt-3 max-h-96 overflow-y-auto border bg-background p-3">
              <MarkdownView content={request.markdown} />
            </div>
          )}

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
