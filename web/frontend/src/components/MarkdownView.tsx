import Markdown, { type Components } from "react-markdown"
import remarkGfm from "remark-gfm"
import type { PluggableList } from "unified"


// 知识文档的 YAML frontmatter 交给 Markdown 会散成正文,单独按原样展示
const FRONTMATTER = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/


/**
 * 渲染排查报告与知识库草稿。react-markdown 默认不解析原始 HTML，
 * 文档内容按纯 Markdown 展示，不引入 HTML 注入面。
 *
 * plugins / components 供知识库页注入 wikilink 支持；其余调用点不传，行为不变。
 */
export function MarkdownView({content, plugins, components}: {
  content: string
  plugins?: PluggableList
  components?: Components
}) {
  const matched = FRONTMATTER.exec(content)

  return (
    <div className="markdown-view">
      {matched ? (
        <pre className="markdown-frontmatter">{matched[1]}</pre>
      ) : null}
      <Markdown components={components} remarkPlugins={[remarkGfm, ...(plugins ?? [])]}>
        {matched ? content.slice(matched[0].length) : content}
      </Markdown>
    </div>
  )
}
