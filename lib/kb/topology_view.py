"""Render the offline topology graph as a self-contained HTML viewer."""
from __future__ import annotations

import html
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from lib.envpack import KNOWLEDGE_ENV_SCOPE, active_env_pack


def _json_payload(graph: dict[str, Any]) -> str:
    # The graph is generated locally, but avoid closing the script tag if a
    # future source ever contains the literal sequence in a label or note.
    return json.dumps(graph, ensure_ascii=False, separators=(",", ":")).replace(
        "</", "<\\/"
    )


def _platform_options() -> str:
    pack = active_env_pack()
    options = [
        (key, "跨平台" if key == KNOWLEDGE_ENV_SCOPE else pack.knowledge_cluster(key).short_name)
        for key in pack.knowledge_platforms()
    ]
    return "".join(
        f'<option value="{html.escape(key)}">{html.escape(label)}</option>' for key, label in options
    )


def render_topology_html(graph: dict[str, Any]) -> str:
    """Return a standalone HTML page with service and node topology views."""
    payload = _json_payload(graph)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ops-agent 运维拓扑</title>
<style>
:root {{ color-scheme: light; --bg:#f6f7f9; --panel:#fff; --text:#20242b;
  --muted:#667085; --line:#d9dee7; --accent:#1769aa; --edge:#9aa7b7;
  --warn:#9a6700; --warn-bg:#fff8db; --node:#e8f1fb; }}
* {{ box-sizing:border-box; }} body {{ margin:0; background:var(--bg); color:var(--text);
  font:14px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
header {{ padding:18px 24px 14px; background:var(--panel); border-bottom:1px solid var(--line); }}
h1 {{ margin:0 0 5px; font-size:22px; }} h2 {{ margin:0 0 10px; font-size:16px; }}
.sub {{ color:var(--muted); font-size:12px; }}
.toolbar {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:14px; align-items:center; }}
select,input {{ border:1px solid var(--line); border-radius:5px; background:var(--panel);
  padding:7px 9px; color:var(--text); }} input {{ min-width:260px; }}
.stats {{ display:flex; flex-wrap:wrap; gap:7px; margin-top:12px; }}
.stat {{ padding:5px 8px; border:1px solid var(--line); border-radius:5px; background:var(--bg); }}
.stat b {{ color:var(--accent); }}
.warning {{ margin-top:12px; padding:10px 12px; border:1px solid #e5c46b;
  border-radius:5px; color:var(--warn); background:var(--warn-bg); }}
main {{ padding:18px 24px 30px; display:grid; gap:16px; }}
.panel {{ min-width:0; background:var(--panel); border:1px solid var(--line); border-radius:7px;
  padding:14px; box-shadow:0 1px 2px #1018280d; }}
.wide {{ overflow:auto; }}
.grid {{ display:grid; grid-template-columns:minmax(560px,1.15fr) minmax(420px,.85fr); gap:16px; }}
svg {{ display:block; width:100%; min-width:720px; height:560px; background:#fbfcfe;
  border:1px solid var(--line); border-radius:5px; }}
.dep-edge {{ stroke:var(--edge); stroke-width:1.25; opacity:.85; marker-end:url(#arrow); }}
.service-node {{ fill:var(--node); stroke:#6d9bc8; stroke-width:1; cursor:pointer; }}
.service-node.selected {{ fill:#cfe4f9; stroke:var(--accent); stroke-width:2; }}
.service-label {{ font-size:11px; pointer-events:none; text-anchor:middle; dominant-baseline:middle; }}
.legend {{ margin-top:7px; color:var(--muted); font-size:12px; }}
.table-wrap {{ overflow:auto; max-height:560px; border:1px solid var(--line); border-radius:5px; }}
table {{ width:100%; border-collapse:collapse; font-size:12px; }} th,td {{ padding:7px 8px;
  text-align:left; vertical-align:top; border-bottom:1px solid var(--line); }}
th {{ position:sticky; top:0; background:#f2f4f7; z-index:1; white-space:nowrap; }}
tbody tr {{ cursor:pointer; }} tbody tr:hover,tbody tr.selected {{ background:#eef6ff; }}
code {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:11px;
  overflow-wrap:anywhere; }} .muted {{ color:var(--muted); }}
.details {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }}
.detail-block {{ padding:10px; background:#fbfcfe; border:1px solid var(--line); border-radius:5px; }}
.detail-block h3 {{ margin:0 0 6px; font-size:13px; }} .detail-block ul {{ margin:5px 0 0 18px;
  padding:0; }} .detail-block li {{ margin:3px 0; overflow-wrap:anywhere; }}
@media (max-width:1000px) {{ .grid,.details {{ grid-template-columns:1fr; }} }}
</style>
</head>
<body>
<header>
  <h1>ops-agent 运维集群拓扑</h1>
  <div class="sub">离线派生图 · 节点/组件/服务/endpoint/告警/知识文档 · 依赖箭头方向：依赖方 → 被依赖方</div>
  <div class="toolbar">
    <label>平台 <select id="platform"><option value="all">全部</option>{_platform_options()}</select></label>
    <label>搜索 <input id="search" placeholder="服务、节点、组件、endpoint…"></label>
  </div>
  <div id="stats" class="stats"></div>
  <div id="warning" class="warning" hidden></div>
</header>
<main>
  <div class="grid">
    <section class="panel">
      <h2>服务依赖图</h2>
      <div id="service-graph"></div>
      <div class="legend">点击服务查看其节点、组件、端口、配置、告警和关联文档。当前只绘制知识库明确记录的 depends_on 关系。</div>
    </section>
    <section class="panel">
      <h2>服务基础信息</h2>
      <div class="table-wrap"><table><thead><tr><th>服务</th><th>平台</th><th>组件</th><th>节点</th><th>依赖</th><th>文档</th></tr></thead><tbody id="service-table"></tbody></table></div>
    </section>
  </div>
  <section class="panel wide">
    <h2>节点清单</h2>
    <div class="table-wrap"><table><thead><tr><th>平台</th><th>节点</th><th>主机/IP</th><th>组件</th><th>服务</th></tr></thead><tbody id="node-table"></tbody></table></div>
  </section>
  <section class="panel">
    <h2 id="detail-title">选择一个服务查看详情</h2>
    <div id="details" class="details"><div class="muted">服务详情将在这里显示。</div></div>
  </section>
  <section class="panel">
    <h2>依赖关系覆盖情况</h2>
    <div id="coverage" class="muted"></div>
  </section>
</main>
<script id="topology-data" type="application/json">{payload}</script>
<script>
const GRAPH = JSON.parse(document.getElementById('topology-data').textContent);
const NODES = new Map(GRAPH.nodes.map(n => [n.id, n]));
const EDGES = GRAPH.edges;
const $ = id => document.getElementById(id);
let selectedService = null;

function esc(value) {{
  return String(value ?? '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
}}
function list(value) {{ return Array.isArray(value) ? value : (value == null || value === '' ? [] : [value]); }}
function platformAllowed(platform, selected) {{ return selected === 'all' || platform === selected; }}
function serviceNodes() {{
  const selected = $('platform').value;
  const query = $('search').value.trim().toLowerCase();
  return GRAPH.nodes.filter(n => n.kind === 'service' && platformAllowed(n.platform, selected) &&
    (!query || [n.name, n.platform, JSON.stringify(n.properties)].join(' ').toLowerCase().includes(query)));
}}
function edgesFor(sourceId, relation) {{ return EDGES.filter(e => e.source === sourceId && (!relation || e.relation === relation)); }}
function incoming(targetId, relation) {{ return EDGES.filter(e => e.target === targetId && (!relation || e.relation === relation)); }}
function relatedNodes(serviceId, relation, direction) {{
  const edges = direction === 'in' ? incoming(serviceId, relation) : edgesFor(serviceId, relation);
  return edges.map(e => NODES.get(direction === 'in' ? e.source : e.target)).filter(Boolean);
}}
function serviceSummary(service) {{
  const components = incoming(service.id, 'component_of').map(e => NODES.get(e.source)).filter(Boolean);
  const componentIds = new Set(components.map(n => n.id));
  const hosts = EDGES.filter(e => e.relation === 'hosts_component' && componentIds.has(e.target)).map(e => NODES.get(e.source)).filter(Boolean);
  const docs = incoming(service.id, 'documents').map(e => NODES.get(e.source)).filter(Boolean);
  const upstream = relatedNodes(service.id, 'depends_on', 'out');
  const downstream = relatedNodes(service.id, 'depends_on', 'in');
  return {{components, hosts, docs, upstream, downstream}};
}}
function renderStats() {{
  const s = GRAPH.stats || {{}};
  const entries = [['节点',s.node_count],['边',s.edge_count],['平台',list(s.platforms).length],['集群',s.clusters],['服务',s.services],['告警',s.node_kinds?.alert || 0],['依赖边',s.edge_relations?.depends_on || 0]];
  $('stats').innerHTML = entries.map(([k,v]) => `<span class="stat">${{esc(k)}} <b>${{esc(v)}}</b></span>`).join('');
  const warnings = list(GRAPH.warnings);
  $('warning').hidden = !warnings.length;
  $('warning').innerHTML = warnings.length ? '<b>数据质量提示：</b>' + warnings.map(esc).join('；') : '';
}}
function renderServiceGraph() {{
  const services = serviceNodes();
  const width = Math.max(760, Math.min(1400, $('service-graph').clientWidth || 1000));
  const cols = Math.max(3, Math.floor(width / 190));
  const cellW = width / cols, cellH = 62;
  const rows = Math.max(1, Math.ceil(services.length / cols));
  const height = Math.max(380, rows * cellH + 50);
  const pos = new Map(services.map((n,i) => [n.id, {{x:(i % cols) * cellW + cellW/2, y:Math.floor(i/cols)*cellH + 35}}]));
  const visible = new Set(services.map(n => n.id));
  const deps = EDGES.filter(e => e.relation === 'depends_on' && visible.has(e.source) && visible.has(e.target));
  let svg = `<svg viewBox="0 0 ${{width}} ${{height}}" role="img" aria-label="服务依赖关系图"><defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto"><path d="M0,0 L8,3 L0,6 z" fill="var(--edge)"></path></marker></defs>`;
  for (const e of deps) {{ const a=pos.get(e.source), b=pos.get(e.target); svg += `<line class="dep-edge" x1="${{a.x}}" y1="${{a.y}}" x2="${{b.x}}" y2="${{b.y}}"></line>`; }}
  for (const n of services) {{ const p=pos.get(n.id), selected=n.id===selectedService ? ' selected' : ''; svg += `<g data-service="${{esc(n.id)}}"><rect class="service-node${{selected}}" x="${{p.x-68}}" y="${{p.y-20}}" width="136" height="40" rx="5"></rect><text class="service-label" x="${{p.x}}" y="${{p.y}}">${{esc(n.name)}} · ${{esc(n.platform)}}</text></g>`; }}
  svg += '</svg>';
  $('service-graph').innerHTML = svg;
  $('service-graph').querySelectorAll('[data-service]').forEach(el => el.addEventListener('click', () => selectService(el.dataset.service)));
}}
function renderServiceTable() {{
  const rows = serviceNodes().sort((a,b) => (a.platform+a.name).localeCompare(b.platform+b.name));
  $('service-table').innerHTML = rows.map(service => {{
    const x=serviceSummary(service), selected=service.id===selectedService ? ' class="selected"' : '';
    return `<tr${{selected}} data-service="${{esc(service.id)}}"><td><b>${{esc(service.name)}}</b></td><td>${{esc(service.platform)}}</td><td>${{x.components.length}}</td><td>${{x.hosts.length}}</td><td>${{x.upstream.length}} / ${{x.downstream.length}}</td><td>${{x.docs.length}}</td></tr>`;
  }}).join('') || '<tr><td colspan="6" class="muted">无匹配服务</td></tr>';
  $('service-table').querySelectorAll('[data-service]').forEach(el => el.addEventListener('click', () => selectService(el.dataset.service)));
}}
function renderNodeTable() {{
  const selected = $('platform').value, query = $('search').value.trim().toLowerCase();
  const rows = GRAPH.nodes.filter(n => n.kind === 'node' && platformAllowed(n.platform, selected) && (!query || [n.name,n.platform,JSON.stringify(n.properties)].join(' ').toLowerCase().includes(query))).sort((a,b)=>(a.platform+a.name).localeCompare(b.platform+b.name));
  $('node-table').innerHTML = rows.map(node => {{
    const componentIds = edgesFor(node.id, 'hosts_component').map(e=>e.target);
    const components = componentIds.map(id=>NODES.get(id)).filter(Boolean);
    const serviceIds = components.flatMap(c=>edgesFor(c.id, 'component_of').map(e=>e.target));
    const services = [...new Set(serviceIds.map(id=>NODES.get(id)?.name).filter(Boolean))];
    return `<tr><td>${{esc(node.platform)}}</td><td><b>${{esc(node.name)}}</b></td><td><code>${{esc(node.properties?.host || '')}}</code></td><td>${{components.map(c=>esc(c.name)).join(', ')}}</td><td>${{services.map(esc).join(', ')}}</td></tr>`;
  }}).join('') || '<tr><td colspan="5" class="muted">无匹配节点</td></tr>';
}}
function block(title, values) {{
  const items = list(values);
  return `<div class="detail-block"><h3>${{esc(title)}}</h3>${{items.length ? '<ul>'+items.map(v=>`<li>${{esc(typeof v === 'string' ? v : JSON.stringify(v))}}</li>`).join('')+'</ul>' : '<span class="muted">无</span>'}}</div>`;
}}
function selectService(id) {{
  selectedService = id;
  const service = NODES.get(id);
  if (!service) return;
  const x = serviceSummary(service);
  const endpointEdges = EDGES.filter(e=>e.relation==='exposes_endpoint' && e.source===id);
  const endpoints = endpointEdges.map(e=>NODES.get(e.target)).filter(Boolean);
  const configs = list(service.properties?.baseline_configs);
  const states = x.components.flatMap(c=>list(c.properties?.observed_state));
  $('detail-title').textContent = `${{service.name}} · ${{service.platform}}`;
  $('details').innerHTML = [
    block('组件', x.components.map(c=>c.name)),
    block('节点', x.hosts.map(n=>`${{n.name}} (${{n.properties?.host || '—'}})`)),
    block('Endpoint', endpoints.map(e=>`${{e.name}} ${{e.properties?.url || e.properties?.host || ''}}:${{e.properties?.port || ''}}`)),
    block('上游依赖', x.upstream.map(n=>n.name)),
    block('下游服务', x.downstream.map(n=>n.name)),
    block('端口/进程状态', x.components.flatMap(c=>[...list(c.properties?.observed_ports).map(p=>`${{c.name}} port ${{p}}`), ...list(c.properties?.processes).map(p=>`${{c.name}} process ${{p}}`), ...list(c.properties?.process_statuses).map(p=>`${{c.name}} process_status ${{p}}`)])),
    block('关键配置', configs),
    block('关联文档', x.docs.map(d=>d.properties?.path || d.name)),
    block('实测状态样本', states.slice(0, 20)),
  ].join('');
  renderServiceGraph(); renderServiceTable();
}}
function renderCoverage() {{
  const coverage = GRAPH.coverage || {{}};
  const dependency = coverage.service_dependencies || {{}};
  const baseline = coverage.service_baseline || {{}};
  const observation = coverage.component_observation || {{}};
  const missing = list(dependency.services_without_evidence);
  $('coverage').innerHTML = `<p>服务依赖：已登记 ${{dependency.documented_edge_count || 0}} 条边，涉及 ${{list(dependency.services_with_evidence).length}} 个服务；尚无明确依赖证据 ${{missing.length}} 个服务。</p><p>服务基础信息：${{baseline.with_baseline || 0}} / ${{baseline.operational_service_count || 0}} 个 HD/IoT 服务已有 baseline。</p><p>组件观测：${{observation.observed_in_platform_config || 0}} 个组件来自平台节点配置，另有 ${{list(observation.baseline_only).length}} 个仅存在于历史 baseline。</p><details><summary>查看缺少依赖证据的服务</summary><p>${{missing.length ? missing.map(esc).join('、') : '无'}}</p></details><p class="muted">“无依赖证据”不代表没有依赖。后续补录应附来源、用途、采集时间和置信度；不要根据组件同机部署自动推断服务调用关系。</p>`;
}}
function render() {{ renderStats(); renderServiceGraph(); renderServiceTable(); renderNodeTable(); renderCoverage(); if (selectedService && NODES.has(selectedService)) selectService(selectedService); }}
$('platform').addEventListener('change', render); $('search').addEventListener('input', render); render();
</script>
</body>
</html>
"""


def write_topology_html(graph: dict[str, Any], output: Path) -> Path:
    """Atomically write the standalone topology viewer."""
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(fd)
    temporary = Path(raw_temp)
    try:
        temporary.write_text(render_topology_html(graph), encoding="utf-8")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
