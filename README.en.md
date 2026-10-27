# ops-agent

A general-purpose OPS console: the knowledge base routes alerts, read-only playbook scripts run first, and an LLM agent
steps in on demand. Everything environment-specific lives in an **environment pack**; data sources and actions plug in
as **MCP plugins**.

[中文](README.md) · [Architecture (zh)](docs/architecture.md) · [Changelog](CHANGELOG.md) · [Roadmap](ROADMAP.md)

## What it does

- **Alerts and resources** — polls Prometheus (via an MCP plugin) and pack-provided alert sources, classifies, dedups
  and notifies; collects node/JVM/business metrics and flags capacity risks
- **Troubleshooting agent** — one runtime plans and reasons over evidence; the harness owns safety: every node command
  goes through `SSHPool.run()` with red-line filtering and a command guard, write operations need human approval and
  an oplog, and conclusions are checked against recorded raw output
- **Knowledge base** — Markdown with frontmatter is the single source of truth for alert routing, symptom recall and
  the agent's context pack; drafts are promoted only after independent confirmation
- **Work items, worklog and weekly report** — track what to do, attach tasks and logs, summarize a period with an LLM
  into a DOCX template
- **Windows tray shell and Linux CLI** — signed self-updates with automatic rollback

## Quick start (example environment)

```bash
git clone <this repo> ops-agent && cd ops-agent
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cd web/frontend && npm ci && npm run build && cd ../..
.venv/bin/python -m pytest
.venv/bin/python scripts/web_server.py      # http://127.0.0.1:8420
```

`envs/example/` is a fictional environment (clusters `alpha`/`beta`, documentation-range addresses, local placeholder
plugins) used by the core tests and for a first look at the UI.

## Environment packs

A pack holds `env.yaml` (clusters, roles, timezone, knowledge vocabulary, official plugins, extensions), inventories and
topology snapshots under `config/`, read-only `playbooks/`, red-line additions, a noun list for leak scanning, and
optional Python extensions. A pack is application data, not part of the code or the release package: put it at
`<data root>/env/`, or point `<data root>/config/paths.local.yaml` at it with `env_pack:` (absolute, or relative to the
data root). Without either, the bundled example pack is used and the UI says so.

Extension points: `node_credentials`, `credential_profiles`, `alert_sources`, `graphs` — the example pack ships a
minimal implementation of the last three.

`scripts/noun_lint.py` scans every tracked file outside `envs/` with the pack's noun list plus built-in patterns
(private addresses, e-mail addresses, credential shapes); the pre-commit hook scans staged content and CI scans all.

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE). `desktop/internal/webview2/` is third-party code under MIT.
