import { useState } from "react"
import { CheckCircle2, ShieldAlert, XCircle } from "lucide-react"
import { Button } from "@/components/ui/button"
import type { ApprovalRequest } from "@/types"


type ApprovalCardProps = {
  request: ApprovalRequest
  decided: boolean | null
  manual: boolean
  busy: boolean
  onDecision: (approvalId: string, approved: boolean) => Promise<void>
}


export function ApprovalCard({
  request,
  decided,
  manual,
  busy,
  onDecision,
}: ApprovalCardProps) {
  const [confirming, setConfirming] = useState(false)

  return (
    <section className="triage-approval" aria-label="受控动作审批">
      <div className="flex items-start gap-3">
        <ShieldAlert className="mt-0.5 size-5 shrink-0 text-[#b7791f]" aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <strong className="text-sm">受控动作等待审批</strong>
            <span className="font-mono text-[11px] text-muted-foreground">{request.approvalId}</span>
          </div>
          <dl className="triage-approval-facts">
            <div><dt>动作</dt><dd className="font-mono">{request.actionId || "-"}</dd></div>
            <div><dt>命令</dt><dd><code>{request.command || "-"}</code></dd></div>
            <div><dt>影响</dt><dd>{request.impact || "未提供"}</dd></div>
            <div><dt>回滚</dt><dd>{request.rollback || "未提供"}</dd></div>
          </dl>
          {decided !== null ? (
            <p className={`mt-4 flex items-center gap-2 text-sm ${decided ? "text-[#247a56]" : "text-destructive"}`}>
              {decided ? <CheckCircle2 className="size-4" /> : <XCircle className="size-4" />}
              {decided ? "已批准，任务将继续排队" : "已拒绝，任务将按拒绝结果继续处理"}
            </p>
          ) : !manual ? (
            <p className="mt-4 text-xs text-muted-foreground">自动任务只展示动作建议，不能在中台批准。</p>
          ) : confirming ? (
            <div className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t pt-4">
              <p className="text-sm font-medium text-[#9a6518]">确认批准这个登记动作？</p>
              <div className="flex gap-2">
                <Button disabled={busy} onClick={() => setConfirming(false)} size="sm" variant="outline">
                  返回
                </Button>
                <Button
                  disabled={busy}
                  onClick={() => void onDecision(request.approvalId, true)}
                  size="sm"
                >
                  确认批准
                </Button>
              </div>
            </div>
          ) : (
            <div className="mt-4 flex justify-end gap-2 border-t pt-4">
              <Button
                disabled={busy}
                onClick={() => void onDecision(request.approvalId, false)}
                size="sm"
                variant="outline"
              >
                拒绝
              </Button>
              <Button disabled={busy} onClick={() => setConfirming(true)} size="sm">
                批准
              </Button>
            </div>
          )}
        </div>
      </div>
    </section>
  )
}
