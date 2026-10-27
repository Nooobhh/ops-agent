#!/usr/bin/env bash
# 安装各仓的 pre-commit hook:主仓管专有名词扫描与红线双向锁,知识库与 records 两个
# 独立 vault 仓各管自己的 lint,环境包仓(独立 git 仓时)跑包内测试。仓缺失时静默跳过(新机可能尚未 clone)。
# 知识库、records 与环境包可在 ops-agent 之外(config/paths.local.yaml 的 knowledge_root/records_root/env_pack),
# 其 hook 安装时写死主仓绝对路径;任一仓搬家后需重跑本脚本。
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"

install_hook() {  # $1=仓目录 $2=hook 正文
  local dir="$1"
  if [ ! -d "$dir/.git" ]; then
    echo "跳过 $dir(不是 git 仓)"
    return
  fi
  local hook="$dir/.git/hooks/pre-commit"
  printf '%s\n' "$2" > "$hook"
  chmod +x "$hook"
  echo "已安装 $hook"
}

# ── 主仓:暂存区专有名词/敏感信息扫描(公开仓日常提交的主防线)+ 红线双向锁 ──
# worktree 共用本 hook:一律用主仓 .venv;词表取主仓数据根配置的环境包(worktree 可能没配 env_pack);
# 旧分支的 noun_lint 不认 --staged 时跳过扫描
install_hook "$REPO" "#!/usr/bin/env bash
set -e
MAIN=\"$REPO\""'
REPO="$(git rev-parse --show-toplevel)"
cd "$REPO"
PY="$MAIN/.venv/bin/python"
[ -x "$PY" ] || PY="$MAIN/.venv/Scripts/python.exe"

if grep -q -- "--staged" scripts/noun_lint.py 2>/dev/null; then
  if ! LINT=$(OPS_AGENT_DATA_ROOT="$MAIN" "$PY" scripts/noun_lint.py --staged --check); then
    echo "[pre-commit] 暂存内容命中专有名词或敏感信息(示例值行内写 noun-lint: allow):" >&2
    echo "$LINT" >&2
    exit 1
  fi
fi

REDLINE_CHANGED=$(git diff --cached --name-only | grep -E "^lib/data/filter_rules\.yaml$" || true)
if [ -z "$REDLINE_CHANGED" ]; then
  exit 0
fi

echo "[pre-commit] filter_rules.yaml 变动,跑 kb-lint 校验红线双向锁..."
"$PY" -m lib.kb --repo-root "$REPO"'

# ── vault 仓公共前导:定位主仓 .venv(Windows 工作机走 Scripts/) ────────
# 首行由各仓自带:两个 vault 仓都可能在主仓外,写死主仓路径
VAULT_COMMON='PY="$MAIN/.venv/bin/python"
[ -x "$PY" ] || PY="$MAIN/.venv/Scripts/python.exe"
if [ ! -x "$PY" ]; then
  echo "[pre-commit] 找不到主仓 .venv,跳过 lint" >&2
  exit 0
fi
cd "$MAIN"'

PY_MAIN="$REPO/.venv/bin/python"
[ -x "$PY_MAIN" ] || PY_MAIN="$REPO/.venv/Scripts/python.exe"
KNOWLEDGE="$(cd "$REPO" && "$PY_MAIN" -c 'from pathlib import Path; from lib.kb.bases import resolve_knowledge_root; print(resolve_knowledge_root(Path(".")))')"

install_hook "$KNOWLEDGE" "#!/usr/bin/env bash
set -e
MAIN=\"$REPO\"
$VAULT_COMMON"'
echo "[pre-commit] 检查知识库写入入口..."
"$PY" scripts/check_kb_entrypoints.py --repo-root "$MAIN"
echo "[pre-commit] 跑 kb-lint..."
"$PY" -m lib.kb --repo-root "$MAIN"
echo "[pre-commit] 重建 wiki..."
"$PY" scripts/build_wiki.py --repo-root "$MAIN"
echo "[pre-commit] 检查知识索引是否已暂存..."
"$PY" scripts/check_kb_indexes.py --repo-root "$MAIN"'

RECORDS="$(cd "$REPO" && "$PY_MAIN" -c 'from pathlib import Path; from lib.data_root import resolve_records_root; print(resolve_records_root(Path(".")).resolve())')"

install_hook "$RECORDS" "#!/usr/bin/env bash
set -e
MAIN=\"$REPO\"
RECORDS=\"$RECORDS\"
$VAULT_COMMON"'
echo "[pre-commit] 跑 record lint..."
"$PY" -m lib.record lint "$RECORDS/troubleshoot"'

# ── 环境包仓:用主仓 .venv 跑包内 tests(含「核心干净」扫描);本仓自带的示例包不装 ──
ENV_PACK="$(cd "$REPO" && "$PY_MAIN" -c 'from pathlib import Path; from lib.envpack import resolve_env_pack_dir; print(resolve_env_pack_dir(Path(".").resolve()).resolve())')"

case "$ENV_PACK" in
  "$REPO"/*) echo "跳过 $ENV_PACK(本仓内的环境包)" ;;
  *) install_hook "$ENV_PACK" "#!/usr/bin/env bash
set -e
MAIN=\"$REPO\""'
PY="$MAIN/.venv/bin/python"
[ -x "$PY" ] || PY="$MAIN/.venv/Scripts/python.exe"
if [ ! -x "$PY" ]; then
  echo "[pre-commit] 找不到主仓 .venv,跳过环境包测试" >&2
  exit 0
fi
cd "$(git rev-parse --show-toplevel)"
echo "[pre-commit] 跑环境包测试..."
env -u OPS_AGENT_KNOWLEDGE_ROOT -u OPS_AGENT_DATA_ROOT OPS_AGENT_CORE="$MAIN" "$PY" -m pytest -q -p no:cacheprovider tests' ;;
esac
