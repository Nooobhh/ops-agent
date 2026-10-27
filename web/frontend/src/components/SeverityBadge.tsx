import { Badge } from "@/components/ui/badge"

type Props = { severity?: string | null }

export function SeverityBadge({ severity }: Props) {
  const normalized = (severity || "unknown").toLowerCase()
  return (
    <Badge className="status-badge" data-status={normalized} variant="outline">
      {normalized === "unknown" ? "-" : severity}
    </Badge>
  )
}
