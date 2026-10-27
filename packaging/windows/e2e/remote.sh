#!/usr/bin/env bash
# mac 侧驱动 Windows 实机验收(SSH 进不了桌面会话:GUI 动作经 Interactive 计划任务拉进用户桌面)。
#   remote.sh <ssh 主机> <安装根,如 D:\OpsAgent-m9> <动作> [参数...]
#   push                     把本目录脚本拷到 <安装根>\e2e\
#   install <首装 zip>       拷到主机并解压成 <安装根>(须不存在)
#   seed-config <本地目录>   把目录里的文件拷到 <安装根>\data\(如 state\acquisition-settings.json)
#   start [外壳参数...]      在交互桌面拉起 OpsAgent.exe
#   quit                     在交互桌面跑 OpsAgent.exe --quit(停中台并退出外壳)
#   ui <动作> [参数]         在交互桌面跑 ui.ps1,打印结果并把截图取回 $E2E_OUT(缺省 ./e2e-out)
#   ps                       安装根下的进程、内存(工作集/私有)与 8420 监听
#   logs                     shell.log 与 web_server.log 尾部
#   kill                     兜底:按进程树结束安装根下的全部进程
#   cleanup                  删 e2e 计划任务
set -euo pipefail
host=$1 root=$2 action=$3
shift 3
here=$(cd "$(dirname "$0")" && pwd)
unix_root=${root//\\//}
out=${E2E_OUT:-./e2e-out}

ps_run() {  # PowerShell 文本 → EncodedCommand(不走 stdin:中文按 GBK 读会乱)
  local b64
  b64=$(printf '%s' "\$ProgressPreference='SilentlyContinue';[Console]::OutputEncoding=[Text.Encoding]::UTF8;$1" |
    iconv -f UTF-8 -t UTF-16LE | base64 | tr -d '\n')
  ssh -n "$host" "powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand $b64"  # -n:别吃掉调用方循环的 stdin
}

interactive() {  # <任务名> <exe> <参数> [等待秒数]
  ps_run "& '$root\\e2e\\interactive.ps1' -Name '$1' -Exe '$2' -Arguments '$3' -WorkingDirectory '$root' -WaitSeconds ${4:-0}"
}

case $action in
  push)
    ps_run "New-Item -ItemType Directory -Force '$root\\e2e' | Out-Null"
    scp -q "$here"/interactive.ps1 "$here"/ui.ps1 "$here"/hidden.vbs "$host:$unix_root/e2e/"
    ;;
  install)
    zip=$1
    ps_run "if (Test-Path '$root') { throw '$root 已存在' }; New-Item -ItemType Directory -Force '$root.unpack' | Out-Null"
    scp -q "$zip" "$host:$unix_root.unpack/install.zip"
    # Win10 自带 bsdtar 解 zip 比 Expand-Archive 快得多
    ps_run "Set-Location '$root.unpack'; tar -xf install.zip; if (\$LASTEXITCODE) { throw 'tar 失败' }
      Set-Location (Split-Path '$root'); Move-Item '$root.unpack\\OpsAgent' '$root'; Remove-Item -Recurse -Force '$root.unpack'
      Get-ChildItem '$root','$root\\app' | Select-Object FullName | Format-Table -HideTableHeaders | Out-String"
    ;;
  seed-config)
    src=$1
    (cd "$src" && find . -type f) | while read -r f; do
      rel=${f#./}
      dir=$(dirname "$rel")
      ps_run "New-Item -ItemType Directory -Force '$root\\data\\${dir//\//\\}' | Out-Null"
      scp -q "$src/$rel" "$host:$unix_root/data/$rel"
      echo "seeded data/$rel"
    done
    ;;
  start)
    interactive OpsAgentE2E-shell "$root\\OpsAgent.exe" "$*"
    ;;
  quit)
    interactive OpsAgentE2E-quit "$root\\OpsAgent.exe" "--quit" 120
    ;;
  ui)
    act=$1 arg=${2:-}
    interactive OpsAgentE2E-ui "wscript.exe" "\"$root\\e2e\\hidden.vbs\" \"$root\\e2e\\ui.ps1\" -Root \"$root\" -Action $act -Arg \"$arg\"" 180
    ps_run "Get-Content '$root\\e2e-out\\out-$act.txt' -Encoding UTF8"
    mkdir -p "$out"
    scp -q "$host:$unix_root/e2e-out/shot-*.png" "$out/" 2>/dev/null || true
    ps_run "Remove-Item '$root\\e2e-out\\shot-*.png' -ErrorAction SilentlyContinue"
    ;;
  ps)
    ps_run "Get-CimInstance Win32_Process | Where-Object { \$_.CommandLine -like '*$root*' } | ForEach-Object {
        \$p = Get-Process -Id \$_.ProcessId -ErrorAction SilentlyContinue
        [pscustomobject]@{ PID = \$_.ProcessId; PPID = \$_.ParentProcessId; Name = \$_.Name
          WS_MB = [math]::Round(\$p.WorkingSet64 / 1MB, 1); Private_MB = [math]::Round(\$p.PrivateMemorySize64 / 1MB, 1)
          Cmd = (\$_.CommandLine -replace '.*\\\\(scripts\\\\[a-z_]+\\.py).*', '\$1') } } |
      Format-Table -AutoSize | Out-String -Width 160
      Get-NetTCPConnection -State Listen -LocalPort 8420 -ErrorAction SilentlyContinue | Select-Object LocalAddress, OwningProcess | Format-Table | Out-String"
    ;;
  logs)
    ps_run "'---- shell.log'; Get-Content '$root\\shell\\shell.log' -Encoding UTF8 -Tail ${1:-40} -ErrorAction SilentlyContinue
      '---- web_server.log'; Get-Content '$root\\data\\state\\web_server.log' -Encoding UTF8 -Tail ${1:-40} -ErrorAction SilentlyContinue"
    ;;
  kill)
    ps_run "Get-CimInstance Win32_Process | Where-Object { \$_.CommandLine -like '*$root*' -and \$_.Name -ne 'powershell.exe' } |
      ForEach-Object { taskkill /PID \$_.ProcessId /T /F 2>&1 | Out-Null; \"killed \$(\$_.ProcessId) \$(\$_.Name)\" }"
    ;;
  cleanup)
    ps_run "Get-ScheduledTask -TaskName 'OpsAgentE2E-*' -ErrorAction SilentlyContinue | ForEach-Object {
      Unregister-ScheduledTask -TaskName \$_.TaskName -Confirm:\$false; \"removed task \$(\$_.TaskName)\" }"
    ;;
  *)
    echo "未知动作 $action" >&2
    exit 2
    ;;
esac
