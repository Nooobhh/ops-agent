"""离线 wiki 渲染器:routing + knowledge md → 单文件 HTML(自包含,零外部依赖)。

build_payload 纯函数可测;render_wiki 产出完整 HTML 字符串,写盘由调用方负责。
单篇渲染失败降级为 <pre> 原文 + warnings,不炸整站。
"""
from __future__ import annotations

import html as _html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import markdown

from .bases import iter_documents
from .frontmatter import _FM_RE, load_doc

_MD_EXT = ["tables", "fenced_code"]
_TAG_RE = re.compile(r"<[^>]+>")
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)
_META_KEYS = (
    "title", "platform", "service", "problem", "symptoms", "playbook", "playbooks",
    "diagnostic_paths", "knowledge_layer", "entrypoints", "profile",
    "execution_scope", "confidence", "observed_at", "valid_until", "status", "type",
)


# knowledge/ 是过 lint+review 的入库内容,视为可信,允许 raw HTML(产物内部使用)
def _render_body(text: str) -> str:
    body = _FM_RE.sub("", text.lstrip("﻿"), count=1)
    return markdown.markdown(body, extensions=_MD_EXT)


# wikilink → 可点内部链接。在渲染后的 HTML 上做且跳过 code 段:
# bash 的 `[[ -f x ]]` 语法只会出现在 pre/code 里,不能误转
_WL_RE = re.compile(r"\[\[([^\[\]|#]+?)(?:#[^\[\]|]*)?(?:\|([^\[\]]*))?\]\]")
_CODE_SEG_RE = re.compile(r"(<pre[^>]*>.*?</pre>|<code[^>]*>.*?</code>)", re.S)


def _linkify(body_html: str) -> str:
    def rep(m: re.Match) -> str:
        target = m.group(1).strip()
        label = (m.group(2) or "").strip() or target
        return (f'<a class="wl" data-wl="{_html.escape(target, quote=True)}">'
                f'{_html.escape(label)}</a>')
    return "".join(
        seg if seg.startswith(("<pre", "<code")) else _WL_RE.sub(rep, seg)
        for seg in _CODE_SEG_RE.split(body_html))


def _extract_h1(body_html: str) -> str | None:
    m = _H1_RE.search(body_html)
    if not m:
        return None
    t = _html.unescape(_TAG_RE.sub("", m.group(1))).strip()
    return t or None


def build_payload(routing: Dict[str, Any], kb_root: Path,
                  ) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """routing.docs → {rel_path: {meta, html, text}};text 是去标签小写全文(搜索用)。"""
    kb_root = Path(kb_root)
    out: Dict[str, Dict[str, Any]] = {}
    warnings: List[str] = []
    for entry in routing.get("docs") or []:
        rel = entry["path"]
        p = kb_root / rel
        meta = {k: entry.get(k) for k in _META_KEYS}
        meta["alerts"] = sorted(
            alertname
            for platform_alerts in (routing.get("alerts") or {}).values()
            for alertname, route in platform_alerts.items()
            if route.get("doc") == rel)
        meta["verified_by"] = meta["verified_at"] = None
        meta["doc_title"] = None            # 正文首个 H1(人类可读标题,导航/搜索用)
        try:
            raw_text = p.read_text(encoding="utf-8")
            doc = load_doc(p)
            vb = doc.raw.get("verified_by")
            va = doc.raw.get("verified_at")
            meta["verified_by"] = str(vb) if vb is not None else None
            meta["verified_at"] = str(va) if va is not None else None
            body_html = _linkify(_render_body(raw_text))
            meta["doc_title"] = _extract_h1(body_html)
        except Exception as e:                     # 单篇降级,不炸整站
            warnings.append(f"{rel}: {type(e).__name__}: {e}")
            try:
                src = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                src = "(文件不可读)"
            body_html = f"<pre>{_html.escape(src)}</pre>"
        text = _TAG_RE.sub(" ", body_html)
        text = _html.unescape(re.sub(r"\s+", " ", text)).strip().lower()
        out[rel] = {"meta": meta, "html": body_html, "text": text}

    # 服务 Overview:_index.md 不在 routing.docs 里(候选表排除 index),wiki 单独纳入。
    # platform/service 从路径推导;根 _index(master-index)与 wiki 首页重复,跳过
    for item in iter_documents(kb_root):
        p, inner = item.path, item.inner
        if p.name != "_index.md":
            continue
        if item.base.layout == "flat" and len(inner.parts) == 1:
            platform, service = item.base.platform, item.base.service
        elif item.base.layout == "platform_service" and len(inner.parts) >= 3:
            platform, service = inner.parts[0], inner.parts[1]
        else:
            continue
        rel = item.doc_id
        meta = {"title": "_index", "platform": platform, "service": service,
                "problem": None, "symptoms": [], "playbook": None, "status": None,
                "type": "index", "alerts": [], "verified_by": None,
                "verified_at": None, "doc_title": None}
        try:
            body_html = _linkify(_render_body(p.read_text(encoding="utf-8")))
            meta["doc_title"] = _extract_h1(body_html)
        except Exception as e:
            warnings.append(f"{rel}: {type(e).__name__}: {e}")
            try:
                src = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                src = "(文件不可读)"
            body_html = f"<pre>{_html.escape(src)}</pre>"
        text = _TAG_RE.sub(" ", body_html)
        text = _html.unescape(re.sub(r"\s+", " ", text)).strip().lower()
        out[rel] = {"meta": meta, "html": body_html, "text": text}
    return out, warnings


def render_wiki(routing: Dict[str, Any], kb_root: Path, platform_order: Tuple[str, ...] = ()) -> str:
    docs, warnings = build_payload(routing, kb_root)
    n_docs = sum(1 for d in docs.values() if d["meta"]["type"] != "index")
    n_verified = sum(1 for d in docs.values() if d["meta"]["status"] == "verified")
    alerts = {
        platform: {alertname: route["doc"] for alertname, route in platform_alerts.items()}
        for platform, platform_alerts in (routing.get("alerts") or {}).items()
    }
    alert_count = sum(len(platform_alerts) for platform_alerts in alerts.values())
    built_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    data_json = json.dumps({"docs": docs, "alerts": alerts, "platform_order": list(platform_order)},
                           ensure_ascii=False).replace("</", "<\\/")
    warn_html = ""
    if warnings:
        items = "".join(f"<li>{_html.escape(w)}</li>" for w in warnings)
        warn_html = f'<div class="warns"><b>构建警告</b><ul>{items}</ul></div>'
    # 单趟替换:占位符互不扫描彼此的替换结果,JSON 内容里的字面 token 不被污染
    subs = {"__N_DOCS__": str(n_docs), "__N_VERIFIED__": str(n_verified),
            "__N_ALERTS__": str(alert_count), "__BUILT_AT__": built_at,
            "__WARNINGS__": warn_html, "__DATA__": data_json}
    pattern = re.compile("|".join(re.escape(k) for k in subs))
    return pattern.sub(lambda m: subs[m.group(0)], _TEMPLATE)


_TEMPLATE = r"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ops-agent 知识库</title>
<script>(()=>{let t=null;try{t=localStorage.getItem('kb-theme')}catch(e){}
document.documentElement.dataset.theme=t||(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light')})()</script>
<style>
:root { --bg:#fff; --fg:#2d3748; --muted:#8593a5; --line:#e4e8ee; --nav-bg:#f7f9fb;
  --top-bg:#1a2332; --top-fg:#e6edf5; --link:#1c4fa0; --sel:#e3ecfb; --code-bg:#f2f5f8;
  --pre-bg:#1a2332; --pre-fg:#d5e0ee; --ok:#2f9e6e; --draft:#cbd5e0;
  --badge-alert-bg:#fdeaea; --badge-alert-fg:#b03636;
  --sb:rgba(125,140,160,.42); --sb-h:rgba(125,140,160,.68); }
:root[data-theme="dark"] { --bg:#151b26; --fg:#d5dde8; --muted:#7a8798; --line:#2a3444;
  --nav-bg:#1a2230; --top-bg:#0e131c; --link:#7fa8e8; --sel:#243652; --code-bg:#232d3d;
  --pre-bg:#0e131c; --badge-alert-bg:#3d2326; --badge-alert-fg:#e08585; }
* { box-sizing:border-box; scrollbar-width:thin; scrollbar-color:var(--sb) transparent; }
::-webkit-scrollbar { width:9px; height:9px; }
::-webkit-scrollbar-thumb { background:var(--sb); border-radius:5px; }
::-webkit-scrollbar-thumb:hover { background:var(--sb-h); }
::-webkit-scrollbar-track, ::-webkit-scrollbar-corner { background:transparent; }
body { margin:0; font-family:-apple-system,'PingFang SC',sans-serif; background:var(--bg);
  color:var(--fg); font-size:14px; }
.top { position:sticky; top:0; z-index:9; display:flex; align-items:center; gap:14px;
  padding:10px 16px; background:var(--top-bg); color:var(--top-fg); }
.top b small { color:var(--muted); font-weight:400; margin-left:8px; }
#q { flex:1; max-width:480px; background:rgba(255,255,255,.13); border:0; border-radius:6px;
  padding:7px 12px; color:var(--top-fg); font-size:13px; outline:none; }
.stats { margin-left:auto; font-size:12px; color:#9fb2ca; white-space:nowrap; }
#theme { flex:none; background:rgba(255,255,255,.13); border:0; border-radius:6px;
  padding:6px 9px; cursor:pointer; font-size:14px; line-height:1; }
#theme:hover { background:rgba(255,255,255,.25); }
.cols { display:flex; min-height:calc(100vh - 46px); }
#nav { width:250px; flex:none; border-right:1px solid var(--line); background:var(--nav-bg);
  padding:10px 0; overflow-y:auto; position:sticky; top:46px; height:calc(100vh - 46px); }
.plat { padding:6px 14px 2px; font-size:11px; letter-spacing:.08em; color:var(--muted);
  font-weight:700; text-transform:uppercase; cursor:pointer; user-select:none; }
.plat:hover { color:var(--link); }
.svc { display:flex; gap:5px; padding:4px 14px; cursor:pointer; user-select:none; }
.svc:hover { background:var(--sel); }
.svc.on { background:var(--sel); color:var(--link); }
.svc .tw { flex:none; width:13px; }
.svc .nm:hover { color:var(--link); }
.docln { display:flex; align-items:center; gap:7px; padding:3px 14px 3px 30px;
  cursor:pointer; color:var(--fg); white-space:nowrap; font-size:13px; }
.docln .lbl { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; }
.docln:hover { background:var(--sel); }
.docln.on { background:var(--sel); color:var(--link); }
.dot { width:7px; height:7px; border-radius:50%; flex:none; background:var(--draft); }
.dot.v { background:var(--ok); }
#art { flex:1; min-width:0; padding:22px 34px; max-width:860px; overflow-wrap:break-word; }
#art h1 { font-size:21px; margin:0 0 6px; }
#art h2 { font-size:16px; margin:20px 0 8px; padding-bottom:4px;
  border-bottom:1px solid var(--line); }
#art table { border-collapse:collapse; margin:8px 0; display:block; overflow-x:auto; }
#art th, #art td { border:1px solid var(--line); padding:5px 10px; text-align:left; }
#art code { background:var(--code-bg); border-radius:3px; padding:1px 5px; font-size:12.5px;
  overflow-wrap:anywhere; }
#art pre { background:var(--pre-bg); color:var(--pre-fg); border-radius:6px;
  padding:10px 14px; overflow-x:auto; }
#art pre code { background:none; color:inherit; }
#art a { color:var(--link); }
.wl { cursor:pointer; border-bottom:1px dashed var(--link); }
.wl.dead { color:var(--muted); border-bottom:1px dashed var(--muted); cursor:default; }
.crumb { font-size:12px; color:var(--muted); margin-bottom:4px; }
.bdg { display:inline-block; font-size:11px; border-radius:4px; padding:2px 8px;
  margin:0 6px 6px 0; border:1px solid var(--line); color:var(--muted); }
.bdg.v { color:var(--ok); border-color:var(--ok); }
.bdg.alert { background:var(--badge-alert-bg); color:var(--badge-alert-fg);
  border-color:transparent; }
.bdg.pb { font-family:ui-monospace,monospace; color:var(--link); }
#side { width:210px; flex:none; border-left:1px solid var(--line); padding:18px 14px;
  font-size:12.5px; position:sticky; top:46px; height:calc(100vh - 46px); overflow-y:auto; }
#side h4 { font-size:11px; letter-spacing:.08em; color:var(--muted); margin:0 0 6px; }
#toc a { display:block; padding:2px 0; color:var(--link); text-decoration:none; }
#toc a.h3 { padding-left:12px; }
#meta { margin-top:16px; border-top:1px solid var(--line); padding-top:10px; }
#meta dt { color:var(--muted); margin-top:6px; }
#meta dd { margin:1px 0 0; word-break:break-all; }
.home-table { border-collapse:collapse; width:100%; margin-top:10px; }
.home-table th, .home-table td { border-bottom:1px solid var(--line); padding:6px 10px;
  text-align:left; }
.home-table td a { color:var(--link); cursor:pointer; }
.hit { padding:8px 0; border-bottom:1px solid var(--line); }
.hit a { color:var(--link); font-weight:600; cursor:pointer; }
.hit p { margin:3px 0 0; color:var(--muted); font-size:12.5px; }
.warns { margin-top:20px; padding:10px 14px; border:1px solid var(--badge-alert-fg);
  border-radius:6px; font-size:12px; color:var(--badge-alert-fg); }
@media (max-width: 900px) { #side { display:none; } #nav { width:200px; } }
</style></head><body>
<div class="top"><b>ops-agent 知识库<small>__BUILT_AT__ 构建</small></b>
<input id="q" placeholder="搜索标题 / 告警名 / 症状 / 正文…(⌘K)">
<span class="stats" data-stat-docs="__N_DOCS__" data-stat-verified="__N_VERIFIED__"
 data-stat-alerts="__N_ALERTS__"><b>__N_DOCS__</b> 文档 · <b>__N_VERIFIED__</b> verified
 · <b>__N_ALERTS__</b> 告警</span>
<button id="theme" title="切换浅色 / 深色主题">🌙</button></div>
<div class="cols"><nav id="nav"></nav><main id="art"></main>
<aside id="side"><h4>本页目录</h4><div id="toc"></div>
<div id="meta"></div></aside></div>
<div style="padding:0 34px 20px">__WARNINGS__</div>
<script type="application/json" id="kb-data">__DATA__</script>
<script>
const DATA = JSON.parse(document.getElementById('kb-data').textContent);
const DOCS = DATA.docs, ALERTS = DATA.alerts;
const PLAT_ORDER = DATA.platform_order || [];
// (platform/service) → 服务级 Overview 页;wikilink 目标解析表(去 .md 相对路径 / 文件名 slug)
const INDEX_OF = {}, BY_REL = {}, BY_SLUG = {};
for (const [path, d] of Object.entries(DOCS)) {
  if (d.meta.type === 'index' && path.split('/').length === 3)
    INDEX_OF[d.meta.platform + '/' + d.meta.service] = path;
  BY_REL[path.replace(/\.md$/, '')] = path;
  BY_SLUG[path.split('/').pop().replace(/\.md$/, '')] = path;
}
const esc = s => String(s ?? '').replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function go(path) {
  const target = '#' + encodeURIComponent(path);
  if (location.hash === target) { document.getElementById('q').value = ''; route(); }
  else location.hash = target;
}

function buildNav() {
  const tree = {};
  for (const [path, d] of Object.entries(DOCS)) {
    if (d.meta.type === 'index') continue;   // Overview 走服务名点击,不占文档列表
    const m = d.meta, plat = m.platform || '?', svc = m.service || '?';
    ((tree[plat] = tree[plat] || {})[svc] = tree[plat][svc] || []).push(path);
  }
  const nav = document.getElementById('nav');
  let h = '';
  const plats = Object.keys(tree).sort(
    (a, b) => PLAT_ORDER.indexOf(a) - PLAT_ORDER.indexOf(b));
  for (const plat of plats) {
    const nPlat = Object.values(tree[plat]).reduce((s, v) => s + v.length, 0);
    h += `<div class="plat" data-plat="${esc(plat)}">▾ ${esc(plat)}(${nPlat})</div>`;
    h += `<div class="pbody" data-pbody="${esc(plat)}">`;
    for (const svc of Object.keys(tree[plat]).sort()) {
      const docs = tree[plat][svc].sort();
      h += `<div class="svc" data-svc="${esc(plat)}/${esc(svc)}"><span class="tw">▾</span>` +
           `<span class="nm" title="打开服务总览">${esc(svc)}(${docs.length})</span></div>`;
      h += `<div class="grp" data-grp="${esc(plat)}/${esc(svc)}">`;
      for (const p of docs) {
        const m = DOCS[p].meta;
        const lbl = m.doc_title || (m.title || p).replace(/^(hd|iot|shared)-/, '');
        h += `<div class="docln" data-path="${esc(p)}" title="${esc(m.doc_title || m.title || p)}">` +
             `<span class="dot ${m.status === 'verified' ? 'v' : ''}"></span>` +
             `<span class="lbl">${esc(lbl)}</span></div>`;
      }
      h += '</div>';
    }
    h += '</div>';
  }
  nav.innerHTML = h;
  nav.addEventListener('click', e => {
    const pl = e.target.closest('.plat');
    if (pl) {
      const body = nav.querySelector(`[data-pbody="${pl.dataset.plat}"]`);
      const open = body.style.display !== 'none';
      body.style.display = open ? 'none' : '';
      pl.textContent = (open ? '▸' : '▾') + pl.textContent.slice(1);
      return;
    }
    const svc = e.target.closest('.svc');
    if (svc) {
      if (e.target.closest('.nm')) {           // 点名字 → 服务总览
        go(INDEX_OF[svc.dataset.svc] || 'svc:' + svc.dataset.svc);
        return;
      }
      const grp = nav.querySelector(`[data-grp="${svc.dataset.svc}"]`);
      const open = grp.style.display !== 'none';
      grp.style.display = open ? 'none' : '';
      svc.querySelector('.tw').textContent = open ? '▸' : '▾';
      return;
    }
    const ln = e.target.closest('.docln');
    if (ln) go(ln.dataset.path);
  });
}

function badges(m) {
  let h = '';
  if (m.type === 'index') return '<span class="bdg">服务总览</span>';
  if (m.status === 'verified')
    h += `<span class="bdg v">verified${m.verified_by ? ' · ' + esc(m.verified_by) : ''}</span>`;
  else if (m.status) h += `<span class="bdg">${esc(m.status)}</span>`;
  for (const a of m.alerts || []) h += `<span class="bdg alert">${esc(a)}</span>`;
  const playbooks = (m.playbooks || []).map(item => item.path || item).filter(Boolean);
  if (playbooks.length) h += `<span class="bdg pb">playbooks: ${esc(playbooks.join('、'))}</span>`;
  else if (m.playbook) h += `<span class="bdg pb">playbook: ${esc(m.playbook)}</span>`;
  return h;
}

function hi(docPath, svcKey) {
  document.querySelectorAll('.docln').forEach(el =>
    el.classList.toggle('on', el.dataset.path === docPath));
  document.querySelectorAll('.svc').forEach(el =>
    el.classList.toggle('on', el.dataset.svc === svcKey));
}

function svcDocTable(plat, svc, excludePath) {
  const rows = Object.entries(DOCS)
    .filter(([p, d]) => d.meta.platform === plat && d.meta.service === svc &&
                        p !== excludePath)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([p, d]) => {
      const m = d.meta;
      const dot = m.type === 'index' ? '' :
        `<span class="dot ${m.status === 'verified' ? 'v' : ''}"></span> `;
      return `<tr><td>${dot}<a data-path="${esc(p)}">` +
             `${esc(m.doc_title || m.title || p)}</a></td>` +
             `<td>${esc(m.type || '')}</td><td>${esc(m.status || '—')}</td></tr>`;
    });
  if (!rows.length) return '<p>(本服务暂无文档)</p>';
  return '<table class="home-table"><tr><th>文档</th><th>类型</th><th>状态</th></tr>' +
         rows.join('') + '</table>';
}

function renderDoc(path) {
  const d = DOCS[path];
  if (!d) return renderHome();
  const m = d.meta;
  const art = document.getElementById('art');
  const isIdx = m.type === 'index';
  const extra = isIdx ?
    `<h2>本服务文档</h2>${svcDocTable(m.platform, m.service, path)}` : '';
  const head = isIdx ?
    `<h1>${esc(m.doc_title || m.service)}</h1>` : `<h1>${esc(m.title || path)}</h1>`;
  const body = isIdx ? d.html.replace(/<h1[^>]*>.*?<\/h1>/s, '') : d.html;
  art.innerHTML = `<div class="crumb">${esc(m.platform)} / ${esc(m.service)}</div>` +
    head + `<div>${badges(m)}</div>` + body + extra;
  art.querySelectorAll('[data-wl]').forEach(el => {
    const t = el.dataset.wl;
    if (!(BY_REL[t] || BY_SLUG[t])) { el.classList.add('dead'); el.title = '目标不存在'; }
  });
  const toc = document.getElementById('toc');
  let th = '';
  art.querySelectorAll('h2, h3').forEach((el, i) => {
    el.id = 'h' + i;
    th += `<a href="#${encodeURIComponent(path)}" class="${el.tagName === 'H3' ? 'h3' : ''}"` +
          ` onclick="event.preventDefault();document.getElementById('h${i}')` +
          `.scrollIntoView({behavior:'smooth'})">${esc(el.textContent)}</a>`;
  });
  toc.innerHTML = th || '<span style="color:var(--muted)">(无小节)</span>';
  document.getElementById('meta').innerHTML = '<h4>元信息</h4><dl>' +
    (m.status ? `<dt>状态</dt><dd>${esc(m.status)}` +
    `${m.verified_at ? '(' + esc(m.verified_at) + ')' : ''}</dd>` +
     '' : '') +
    (m.verified_by ? `<dt>晋升依据</dt><dd>${esc(m.verified_by)}</dd>` : '') +
    `<dt>类型</dt><dd>${esc(m.type || '')}</dd>` +
    (m.knowledge_layer ? `<dt>知识层</dt><dd>${esc(m.knowledge_layer)}</dd>` : '') +
    (m.profile ? `<dt>结构模板</dt><dd>${esc(m.profile)}</dd>` : '') +
    (m.confidence ? `<dt>可信度</dt><dd>${esc(m.confidence)}</dd>` : '') +
    (m.observed_at ? `<dt>观测时间</dt><dd>${esc(m.observed_at)}</dd>` : '') +
    `<dt>源文件</dt><dd>${esc(path)}</dd></dl>`;
  hi(path, isIdx ? m.platform + '/' + m.service : null);
  window.scrollTo(0, 0);
}

function renderSvcPage(key) {          // 无 _index.md 的服务:合成总览页
  const [plat, svc] = key.split('/');
  document.getElementById('art').innerHTML =
    `<div class="crumb">${esc(plat)}</div><h1>${esc(svc)}(${esc(plat)} 平台)</h1>` +
    `<div><span class="bdg">服务总览</span></div>` +
    `<h2>本服务文档</h2>${svcDocTable(plat, svc, null)}` +
    '<p style="color:var(--muted);font-size:12.5px">本服务尚无 _index.md,' +
    '以上为自动生成的文档清单。</p>';
  document.getElementById('toc').innerHTML = '';
  document.getElementById('meta').innerHTML = '<h4>元信息</h4><dl>' +
    `<dt>平台</dt><dd>${esc(plat)}</dd><dt>服务</dt><dd>${esc(svc)}</dd></dl>`;
  hi(null, key);
  window.scrollTo(0, 0);
}

function renderHome() {
  let rows = '';
  // ALERTS 按 platform 分区:{platform: {alertname: docpath}}
  for (const p of Object.keys(ALERTS).sort()) {
    const byName = ALERTS[p] || {};
    for (const a of Object.keys(byName).sort()) {
      rows += `<tr><td><span class="bdg">${esc(p)}</span></td>` +
              `<td><span class="bdg alert">${esc(a)}</span></td>` +
              `<td><a data-path="${esc(byName[a])}">${esc(byName[a])}</a></td></tr>`;
    }
  }
  document.getElementById('art').innerHTML =
    '<h1>告警 → 文档路由</h1><table class="home-table">' +
    '<tr><th>平台</th><th>告警名</th><th>文档</th></tr>' + rows + '</table>';
  document.getElementById('toc').innerHTML = '';
  document.getElementById('meta').innerHTML = '';
  hi(null, null);
}

function search(q) {
  q = q.trim().toLowerCase();
  if (!q) return route();
  const hits = [];
  for (const [path, d] of Object.entries(DOCS)) {
    const m = d.meta;
    let score = 0;
    if ((m.title || '').toLowerCase().includes(q)) score += 3;
    if ((m.doc_title || '').toLowerCase().includes(q)) score += 3;
    if ((m.alerts || []).some(a => a.toLowerCase().includes(q))) score += 3;
    if ((m.symptoms || []).some(s => s.toLowerCase().includes(q))) score += 2;
    const pos = d.text.indexOf(q);
    if (pos >= 0) score += 1;
    if (score) hits.push({path, m, score, pos});
  }
  hits.sort((a, b) => b.score - a.score);
  document.getElementById('art').innerHTML = `<h1>搜索:${esc(q)}(${hits.length})</h1>` +
    hits.map(h => `<div class="hit"><a data-path="${esc(h.path)}">` +
      `${esc(h.m.doc_title || h.m.title || h.path)}</a>` +
      `<p>${h.pos >= 0 ? esc(DOCS[h.path].text.slice(Math.max(0, h.pos - 40),
        h.pos + 60)) : esc((h.m.symptoms || []).join(' · '))}</p></div>`).join('') ||
    '<p>无结果。</p>';
}

function route() {
  const h = decodeURIComponent(location.hash.slice(1));
  if (!h) return renderHome();
  if (h.startsWith('svc:')) return renderSvcPage(h.slice(4));
  renderDoc(h);
}

document.getElementById('art').addEventListener('click', e => {
  const w = e.target.closest('[data-wl]');
  if (w) {
    const p = BY_REL[w.dataset.wl] || BY_SLUG[w.dataset.wl];
    if (p) go(p);
    return;
  }
  const t = e.target.closest('[data-path]');
  if (t) go(t.dataset.path);
});
document.getElementById('q').addEventListener('input', e => search(e.target.value));
document.addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
    e.preventDefault(); document.getElementById('q').focus();
  }
});
window.addEventListener('hashchange', () => {
  document.getElementById('q').value = ''; route();
});
const themeBtn = document.getElementById('theme');
function syncTheme() {
  themeBtn.textContent = document.documentElement.dataset.theme === 'dark' ? '☀️' : '🌙';
}
themeBtn.addEventListener('click', () => {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem('kb-theme', next); } catch (e) {}
  syncTheme();
});
syncTheme();
buildNav(); route();
</script></body></html>"""
