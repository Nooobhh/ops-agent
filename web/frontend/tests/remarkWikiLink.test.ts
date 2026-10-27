import assert from "node:assert/strict"
import test from "node:test"
import type { Root } from "mdast"

import { knowledgeHref, remarkWikiLink } from "../src/lib/remarkWikiLink.ts"
import type { WikiLink } from "../src/types.ts"


const LINKS: WikiLink[] = [
  { target: "alpha-yarn-pressure", label: "alpha-yarn-pressure", resolved_path: "alpha/yarn/alpha-yarn-pressure.md", ambiguous: false },
  { target: "health", label: "health", resolved_path: "beta/emqx/health.md", ambiguous: true },
]

function run(tree: Root, links: WikiLink[] = LINKS): Root {
  remarkWikiLink(links)()(tree)
  return tree
}

function paragraph(value: string): Root {
  return { type: "root", children: [{ type: "paragraph", children: [{ type: "text", value }] }] }
}

function children(tree: Root) {
  return (tree.children[0] as { children: unknown[] }).children as Array<Record<string, any>>
}


test("resolved target becomes an in-app knowledge link", () => {
  const parts = children(run(paragraph("详见 [[alpha-yarn-pressure]] 一文")))
  assert.deepEqual(parts.map((node) => node.type), ["text", "link", "text"])
  assert.equal(parts[0].value, "详见 ")
  assert.equal(parts[1].url, knowledgeHref("alpha/yarn/alpha-yarn-pressure.md"))
  assert.equal(parts[1].children[0].value, "alpha-yarn-pressure")
  assert.equal(parts[2].value, " 一文")
})


test("pipe label overrides the displayed text", () => {
  const parts = children(run(paragraph("[[alpha-yarn-pressure|队列压力]]")))
  assert.equal(parts[0].children[0].value, "队列压力")
  assert.equal(parts[0].url, knowledgeHref("alpha/yarn/alpha-yarn-pressure.md"))
})


test("anchor suffix is dropped when matching the target", () => {
  const parts = children(run(paragraph("[[alpha-yarn-pressure#处置步骤]]")))
  assert.equal(parts[0].type, "link")
  assert.equal(parts[0].url, knowledgeHref("alpha/yarn/alpha-yarn-pressure.md"))
})


test("unresolved target renders as dead text, never a link", () => {
  const parts = children(run(paragraph("见 [[根本不存在]] 处")))
  assert.deepEqual(parts.map((node) => node.type), ["text", "emphasis", "text"])
  assert.deepEqual(parts[1].data.hProperties.className, ["wl-dead"])
  assert.equal(parts[1].children[0].value, "根本不存在")
})


test("ambiguous short name still links but is marked", () => {
  const parts = children(run(paragraph("[[health]]")))
  assert.equal(parts[0].url, knowledgeHref("beta/emqx/health.md"))
  assert.deepEqual(parts[0].data.hProperties.className, ["wl", "wl-ambiguous"])
})


test("inline code is left alone — bash [[ -f x ]] must not become a link", () => {
  const tree: Root = {
    type: "root",
    children: [{
      type: "paragraph",
      children: [
        { type: "text", value: "判断写成 " },
        { type: "inlineCode", value: "[[ -f /tmp/x ]]" },
      ],
    }],
  }
  const parts = children(run(tree))
  assert.deepEqual(parts.map((node) => node.type), ["text", "inlineCode"])
  assert.equal(parts[1].value, "[[ -f /tmp/x ]]")
})


test("fenced code blocks are left alone", () => {
  const tree: Root = {
    type: "root",
    children: [{ type: "code", lang: "bash", value: "if [[ -f /tmp/x ]]; then\n  echo hit\nfi" }],
  }
  run(tree)
  assert.equal(tree.children[0].type, "code")
  assert.equal((tree.children[0] as { value: string }).value.includes("[[ -f /tmp/x ]]"), true)
})


test("wikilink inside an existing link is skipped — nested links are invalid mdast", () => {
  const tree: Root = {
    type: "root",
    children: [{
      type: "paragraph",
      children: [{
        type: "link", url: "https://example.test",
        children: [{ type: "text", value: "[[alpha-yarn-pressure]]" }],
      }],
    }],
  }
  const parts = children(run(tree))
  assert.equal(parts[0].type, "link")
  assert.equal(parts[0].url, "https://example.test")
  assert.equal(parts[0].children[0].value, "[[alpha-yarn-pressure]]")
})


test("multiple links in one paragraph all convert", () => {
  const parts = children(run(paragraph("[[alpha-yarn-pressure]] 与 [[health]] 都要看")))
  assert.deepEqual(parts.map((node) => node.type), ["link", "text", "link", "text"])
})


test("empty link table leaves the tree untouched", () => {
  const tree = run(paragraph("见 [[alpha-yarn-pressure]]"), [])
  assert.deepEqual(children(tree).map((node) => node.type), ["text"])
})
