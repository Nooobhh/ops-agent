#!/bin/bash
# playbook 公共库(bash 3 兼容,节点上运行)。契约:唯一 stdout = 最终 JSON。
# step 的命令输出全量落 $RUN_DIR/<name>.out,JSON 内 raw 截断 4KB。

RUN_DIR="${OPSAGENT_RUN_DIR:-/tmp/opsagent/manual-$$}"
NODE="${OPSAGENT_NODE:-$(hostname)}"
_PB_NAME=""; _PB_STEPS=""; _PB_METRICS=""

pb_init() { _PB_NAME=$1; mkdir -p "$RUN_DIR"; }

_esc() {  # JSON 字符串转义(纯 bash,无 python/jq 依赖)
  local s=$1
  # 先 strip 所有 <0x20 控制字符(ANSI \x1b/backspace/form-feed 等),只保留 \t \n \r 后续被显式转义
  s=$(printf '%s' "$s" | tr -d '\000-\010\013\014\016-\037\177')
  s=${s//\\/\\\\}; s=${s//\"/\\\"}
  s=${s//$'\r'/}; s=${s//$'\t'/\\t}; s=${s//$'\n'/\\n}
  printf '%s' "$s"
}

# step <name> <cmd> <expect描述> [assess函数]
# assess 函数:参数=raw 文件路径;stdout=actual 值;return 0=pass / 非0=fail
# 无 assess 时:pass = 命令 exit code == 0,actual = "exit=<rc>"
step() {
  local name=$1 cmd=$2 expect=$3 assess=${4:-}
  local raw_file="$RUN_DIR/$name.out" rc actual pass_rc
  bash -c "$cmd" >"$raw_file" 2>&1
  rc=$?
  if [ -n "$assess" ]; then
    actual=$("$assess" "$raw_file")
    pass_rc=$?
  else
    actual="exit=$rc"
    pass_rc=$rc
  fi
  local pass=false; [ "$pass_rc" -eq 0 ] && pass=true
  local raw
  if command -v iconv >/dev/null 2>&1; then
    # head -c 可能把多字节 UTF-8 切在 char 中间;iconv -c 丢弃非法字节兜底
    raw=$(head -c 4096 "$raw_file" | iconv -f utf-8 -t utf-8 -c 2>/dev/null)
  else
    raw=$(head -c 4096 "$raw_file")
  fi
  _PB_STEPS="$_PB_STEPS{\"name\":\"$(_esc "$name")\",\"cmd\":\"$(_esc "$cmd")\",\"expect\":\"$(_esc "$expect")\",\"actual\":\"$(_esc "$actual")\",\"pass\":$pass,\"raw\":\"$(_esc "$raw")\"},"
}

# step_opt <name> <cmd> <expect描述> <组件名>
# 「若有」语义:组件不在本节点部署清单($OPSAGENT_COMPONENTS)内 → 不执行不判定,
# 记 pass=true、actual=not-deployed:<组件>;在清单内则等价于无 assess 的 step
# (退出码即判定,进程没了=fail)。
# 清单未注入时退回严格判定 —— 宁可误报,不可把真实故障误判成「本就没部署」。
step_opt() {
  local name=$1 cmd=$2 expect=$3 component=$4
  if [ -z "${OPSAGENT_COMPONENTS:-}" ]; then
    step "$name" "$cmd" "$expect"
    return
  fi
  case ",${OPSAGENT_COMPONENTS}," in
    *",${component},"*)
      step "$name" "$cmd" "$expect" ;;
    *)
      _PB_STEPS="$_PB_STEPS{\"name\":\"$(_esc "$name")\",\"cmd\":\"$(_esc "$cmd")\",\"expect\":\"$(_esc "$expect")\",\"actual\":\"not-deployed:$(_esc "$component")\",\"pass\":true,\"raw\":\"\"}," ;;
  esac
}

# metric <名> <数值>
# 值取不到(工具缺失等)时写 null —— 空值会拼出 "k":, 这种损坏 JSON,
# 让整份结果不可解析,一个缺失的采集项不该毁掉整次执行。
metric() {
  local name=$1 value
  # wc/awk 等常带前后空白(macOS 的 wc -l 尤甚),先去掉再校验
  value=$(printf '%s' "$2" | tr -d ' \t\n\r')
  case "$value" in
    ''|*[!0-9.eE+-]*|*.*.*) value=null ;;
  esac
  _PB_METRICS="$_PB_METRICS\"$(_esc "$name")\":$value,"
}

# emit_verdict <conclusive|needs_llm|insufficient> <P0|P1|P2|ok> <结论提示> [next_playbook]
emit_verdict() {
  local verdict=$1 severity=$2 hint=$3 next=${4:-}
  printf '{"playbook":"%s","node":"%s","verdict":"%s","severity":"%s","conclusion_hint":"%s","steps":[%s],"metrics":{%s}' \
    "$(_esc "$_PB_NAME")" "$(_esc "$NODE")" "$verdict" "$severity" "$(_esc "$hint")" \
    "${_PB_STEPS%,}" "${_PB_METRICS%,}"
  if [ -n "$next" ]; then printf ',"next_playbook":"%s"' "$(_esc "$next")"; fi
  printf ',"raw_dir":"%s"}\n' "$(_esc "$RUN_DIR")"
}
