#!/bin/bash
# playbook: test/toy — 契约自测用
source "${PLAYBOOK_LIB:-$(dirname "$0")/../../playbooks/_lib/playbook_lib.sh}"
pb_init "test/toy"

assess_nonempty() { local v; v=$(head -c 64 "$1"); printf '%s' "$v"; [ -n "$v" ]; }

step "host" "hostname" "非空" assess_nonempty
step "always_fail" "false" "exit=0"
metric answer 42
emit_verdict needs_llm P2 "toy 结论提示"
