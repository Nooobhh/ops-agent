import { Badge } from "@/components/ui/badge"
import { formatDuration } from "@/lib/api"
import {
  buildAlertMetaTokens,
  type AlertMetaInput,
} from "@/lib/alertMeta"

type AlertMetaProps = {
  item: AlertMetaInput
}

export function AlertMeta({ item }: AlertMetaProps) {
  return buildAlertMetaTokens(item).map((token) => (
    token.kind === "tag"
      ? <Badge
          className={token.status === "muted" ? "status-badge" : "status-badge class-badge"}
          data-status={token.status === "muted" ? undefined : token.status}
          key={token.status}
          variant="outline"
        >{token.label}</Badge>
      : <span className="text-xs text-muted-foreground" key="duration">
          已持续 {formatDuration(token.seconds)}
        </span>
  ))
}
