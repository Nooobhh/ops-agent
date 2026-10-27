import { visit, SKIP } from "unist-util-visit"
import type { Nodes, Parents, PhrasingContent, Root, Text } from "mdast"
import type { WikiLink } from "@/types"

// 与后端 lib/kb/browse.py 同源:group(1)=target(anchor 丢弃),group(2)=label
const WIKILINK = /\[\[([^[\]|#]+?)(?:#[^[\]|]*)?(?:\|([^[\]]*))?\]\]/g

export function knowledgeHref(path: string) {
  return `/knowledge/${path}`
}

function liveLink(link: WikiLink): PhrasingContent {
  return {
    type: "link",
    url: link.href || knowledgeHref(link.resolved_path as string),
    children: [{ type: "text", value: link.label }],
    data: {
      hProperties: {
        className: link.ambiguous ? ["wl", "wl-ambiguous"] : ["wl"],
        title: link.ambiguous
          ? `该短名对应多篇文档，跳转到 ${link.resolved_path}`
          : link.href || link.resolved_path as string,
      },
    },
  }
}

// 目标不存在时不生成链接:渲染成灰色不可点文本,与离线 wiki 的 dead 样式一致
function deadLink(label: string): PhrasingContent {
  return {
    type: "emphasis",
    children: [{ type: "text", value: label }],
    data: { hProperties: { className: ["wl-dead"], title: "目标文档不存在" } },
  }
}

function splitText(value: string, byTarget: Map<string, WikiLink>): PhrasingContent[] | null {
  const parts: PhrasingContent[] = []
  let cursor = 0
  WIKILINK.lastIndex = 0
  for (let m = WIKILINK.exec(value); m !== null; m = WIKILINK.exec(value)) {
    const target = m[1].trim()
    const label = (m[2] ?? "").trim() || target
    const link = byTarget.get(target)
    if (m.index > cursor) parts.push({ type: "text", value: value.slice(cursor, m.index) })
    parts.push(link?.resolved_path || link?.href
      ? liveLink({ ...link, label })
      : deadLink(label))
    cursor = m.index + m[0].length
  }
  if (!parts.length) return null
  if (cursor < value.length) parts.push({ type: "text", value: value.slice(cursor) })
  return parts
}

/**
 * 把正文里的 [[wikilink]] 换成站内链接。
 *
 * 在 mdast 上做而不是在 markdown 源或渲染后的 HTML 上做：code 与 inlineCode 是
 * 独立节点类型，访问 text 节点时天然跳过，bash 的 `[[ -f x ]]` 不会被误转
 * —— 离线 wiki 为此要在 HTML 上手工切分 pre/code 段（lib/kb/wiki.py:38-46）。
 */
export function remarkWikiLink(links: WikiLink[]) {
  const byTarget = new Map(links.map((link) => [link.target, link]))
  return () => (tree: Root) => {
    if (!byTarget.size) return
    visit(tree, "text", (node: Text, index: number | undefined, parent: Parents | undefined) => {
      if (!parent || index === undefined) return
      // 链接文本里的 [[x]] 不能再生成链接——嵌套 link 在 mdast 里非法
      if (parent.type === "link" || parent.type === "linkReference") return
      const parts = splitText(node.value, byTarget)
      if (!parts) return
      parent.children.splice(index, 1, ...(parts as Nodes[] as typeof parent.children))
      return [SKIP, index + parts.length]
    })
  }
}
