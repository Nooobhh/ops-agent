import { CheckCircle2, ShieldAlert, XCircle } from "lucide-react"
import { Button } from "@/components/ui/button"
import { commandModeLabel } from "@/lib/triageView"
import type {
  ApprovalScope,
  CommandReviewTargets,
  CommandReviewVerdict,
  KbReviewOutcome,
} from "@/types"

type CommandReviewRequest = {
  approvalId: string
  command: string
  signature: string
  node: string | null
  mode: string
  reason: string
  review: CommandReviewVerdict | null
  targets: CommandReviewTargets
}

type CommandReviewCardProps = {
  request: CommandReviewRequest
  outcome: KbReviewOutcome | null
  manual: boolean
  busy: boolean
  onDecision: (
    approvalId: string,
    decision: "approve" | "reject",
    feedback?: string,
    scope?: ApprovalScope,
  ) => Promise<void>
}

function humanSignature(signature: string): string {
  const suffix = signature.match(/\s+([@#]).*$/)?.[1]
  const core = signature.replace(/\s+[@#].*$/, "").trim() || signature
  const note = suffix === "@"
    ? "（限这条路径）"
    : suffix === "#" ? "（限这段脚本）" : ""
  return `「${core}」这类命令${note}`
}

function scopeLabel(scope: string | undefined): string {
  if (scope === "always") return "永久名单"
  if (scope === "task") return "本次排查"
  return "这一次"
}

function outcomeText(outcome: KbReviewOutcome): string {
  return `${outcome.decision === "approve" ? "已批准" : "已拒绝"} · 生效范围：${scopeLabel(
    (outcome as KbReviewOutcome & {scope?: string}).scope,
  )}`
}

export function CommandReviewCard({
  request,
  outcome,
  manual,
  busy,
  onDecision,
}: CommandReviewCardProps) {
  const choose = (decision: "approve" | "reject", scope: ApprovalScope) =>
    void onDecision(request.approvalId, decision, undefined, scope)

  return (
    <section className="triage-approval" aria-label="命令审核">
      <div className="flex items-start gap-3">
        <ShieldAlert className="mt-0.5 size-5 shrink-0 text-[#b7791f]" aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <strong className="text-sm">命令等待人工审核</strong>
            <span className="font-mono text-[11px] text-muted-foreground">{request.approvalId}</span>
          </div>
          <pre className="mt-3 max-h-72 overflow-auto whitespace-pre-wrap break-words bg-[var(--pre-bg)] p-3 font-mono text-[11px] leading-5 text-[var(--pre-fg)]">
            {request.command || "-"}
          </pre>
          <dl className="triage-approval-facts">
            <div><dt>挂起理由</dt><dd>{request.reason || "未提供"}</dd></div>
            <div><dt>签名</dt><dd>{humanSignature(request.signature)}</dd></div>
            <div><dt>节点</dt><dd>{request.node || "未指定"}</dd></div>
            <div><dt>档位</dt><dd>{commandModeLabel(request.mode)}</dd></div>
          </dl>
          {request.review?.reviewed ? (
            <p className="mt-2 text-xs text-muted-foreground">
              审核模型：{request.review.safe ? "建议安全" : "建议谨慎"} · {request.review.reason || "未提供理由"}
            </p>
          ) : (
            <p className="mt-2 text-xs text-muted-foreground">审核模型未参与</p>
          )}
          {request.targets.paths.length ? (
            <div className="mt-3 text-xs">
              <p className="text-muted-foreground">目标文件</p>
              <ul className="mt-1 list-inside list-disc font-mono">
                {request.targets.paths.map((path) => <li key={path}>{path}</li>)}
              </ul>
            </div>
          ) : null}
          {request.targets.mysql.length ? (
            <div className="mt-3 text-xs">
              <p className="text-muted-foreground">MySQL 目标</p>
              {request.targets.mysql_parsed ? request.targets.mysql.map((item, index) => (
                <p className="mt-1 font-mono" key={`${item.database}-${index}`}>
                  {item.database || "未指定库"}{item.tables.length ? ` · ${item.tables.join(", ")}` : ""}
                </p>
              )) : <p className="mt-1">命令涉及 mysql，未能解析出库表</p>}
            </div>
          ) : null}
          {request.targets.mysql.length === 0 && !request.targets.mysql_parsed ? (
            <p className="mt-3 text-xs text-[#9a6518]">命令涉及 mysql，未能解析出库表</p>
          ) : null}

          {outcome ? (
            <p className={`mt-4 flex items-center gap-2 text-sm ${outcome.decision === "approve" ? "text-[#247a56]" : "text-destructive"}`}>
              {outcome.decision === "approve" ? <CheckCircle2 className="size-4" /> : <XCircle className="size-4" />}
              {outcomeText(outcome)}
            </p>
          ) : !manual ? (
            <p className="mt-4 text-xs text-muted-foreground">自动任务不能在中台裁决</p>
          ) : (
            <div className="mt-4 flex flex-wrap justify-end gap-2 border-t pt-4">
              <Button disabled={busy} onClick={() => choose("reject", "once")} size="sm" variant="outline">拒绝</Button>
              <Button disabled={busy} onClick={() => choose("approve", "once")} size="sm">放行这一次</Button>
              <Button disabled={busy} onClick={() => choose("approve", "task")} size="sm">本次排查放行 {humanSignature(request.signature)}</Button>
              <Button disabled={busy} onClick={() => choose("approve", "always")} size="sm" variant="outline">加入白名单</Button>
              <Button disabled={busy} onClick={() => choose("reject", "always")} size="sm" variant="outline">加入黑名单</Button>
            </div>
          )}
        </div>
      </div>
    </section>
  )
}
