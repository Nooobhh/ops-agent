#!/bin/bash
# playbook: alpha/system/check_memory_pressure
# 关联: knowledge/cluster/alpha/system/alpha-system-memory-pressure.md
# 判定慢性 swap 残留 vs 活跃换页。全部只读。

source "${PLAYBOOK_LIB:-$(dirname "$0")/../../_lib/playbook_lib.sh}"
pb_init "alpha/system/check_memory_pressure"

# hard 判定:swap 使用率 > 50% 且 si/so 均 <100 → 慢性残留(conclusive)
assess_swap_ratio() {
    awk '/^Swap:/ {if ($2>0) printf "%.1f%%\n", $3/$2*100; else print "0.0%"}' "$1"
    ratio=$(awk '/^Swap:/ {if ($2>0) print int($3/$2*100); else print 0}' "$1")
    [ "$ratio" -lt 50 ]
}

assess_vmstat_pages() {
    tail -1 "$1" | awk '{print "si="$7" so="$8}'
    si=$(tail -1 "$1" | awk '{print $7}')
    so=$(tail -1 "$1" | awk '{print $8}')
    [ "$si" -lt 100 ] && [ "$so" -lt 100 ]
}

assess_mem_pct() {
    awk '/^Mem:/ {printf "%.1f%%\n", ($2-$7)/$2*100}' "$1"
    pct=$(awk '/^Mem:/ {printf "%d", ($2-$7)/$2*100}' "$1")
    [ "$pct" -lt 90 ]
}

step "mem_usage" "free -m" "内存使用率 <90%" assess_mem_pct
step "swap_usage" "free -m" "swap 使用率 <50%" assess_swap_ratio
step "vmstat_pages" "vmstat 1 3 | tail -1" "si/so 均 <100(无活跃换页)" assess_vmstat_pages

# 收集辅助 metrics(不参与判定)
metric mem_used_pct "$(free -m | awk '/^Mem:/ {printf "%d", ($2-$7)/$2*100}')"
metric swap_used_pct "$(free -m | awk '/^Swap:/ {if ($2>0) print int($3/$2*100); else print 0}')"

# 判 verdict:
# - swap 高但 si/so 低 → 慢性残留(conclusive P1)
# - swap 高且 si/so 高 → 活跃换页(needs_llm P0)
# - 内存也不高 → 无异常(conclusive ok)
swap_pct=$(free -m | awk '/^Swap:/ {if ($2>0) print int($3/$2*100); else print 0}')
si=$(vmstat 1 2 | tail -1 | awk '{print $7}')

if [ "$swap_pct" -ge 50 ] && [ "$si" -lt 100 ]; then
    emit_verdict conclusive P1 "慢性 swap 残留:swap ${swap_pct}% 但 si/so 无活跃换页"
elif [ "$swap_pct" -ge 50 ] && [ "$si" -ge 100 ]; then
    emit_verdict needs_llm P0 "活跃换页中:swap ${swap_pct}% + si=${si}"
else
    emit_verdict conclusive ok "内存无异常"
fi
