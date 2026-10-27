# 首装冒烟:启动 OpsAgent.exe → 中台就绪、worker 拉起 → app 目录零写入 → 二次启动不多开
# → --quit 后进程树清空、端口释放 → 再次启动拉起 → 退出。
# 用法:pwsh packaging\windows\smoke.ps1 -Root C:\smoke\OpsAgent(先放好 data\config\model.local.yaml 才会拉起 worker)
param([Parameter(Mandatory)][string]$Root, [int]$Port = 8420)
$ErrorActionPreference = 'Stop'
$exe = Join-Path $Root 'OpsAgent.exe'
$version = (Get-Content (Join-Path $Root 'app\current') -Raw).Trim()
$appDir = Join-Path $Root "app\$version"

function Get-Procs([string]$Pattern) {
    # 跑本脚本的 PowerShell 命令行里也有 $Root,排除掉
    @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like $Pattern -and $_.CommandLine -like "*$Root*" -and
        $_.ProcessId -ne $PID -and $_.Name -notin 'pwsh.exe', 'powershell.exe' })
}

function Show-Logs {
    Write-Host '---- shell.log'
    Get-Content "$Root\shell\shell.log" -Encoding UTF8 -ErrorAction SilentlyContinue
    Write-Host '---- web_server.log (tail)'
    Get-Content "$Root\data\state\web_server.log" -Encoding UTF8 -Tail 80 -ErrorAction SilentlyContinue
}

function Wait-Until([scriptblock]$Condition, [int]$Seconds, [string]$What) {
    for ($i = 0; $i -lt $Seconds; $i++) {
        if (& $Condition) { return }
        Start-Sleep -Seconds 1
    }
    Show-Logs
    throw "超时: $What"
}

function Test-Health {
    try { (Invoke-RestMethod "http://127.0.0.1:$Port/api/health" -TimeoutSec 2).status -eq 'ok' } catch { $false }
}

function Test-PortFree {
    -not (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
}

function Quit-Shell {
    $p = Start-Process $exe -ArgumentList '--quit' -PassThru -Wait
    if ($p.ExitCode -ne 0) { Show-Logs; throw "--quit 退出码 $($p.ExitCode)" }
    Wait-Until { (Get-Procs '*').Count -eq 0 } 60 '进程树清空'
    Wait-Until { Test-PortFree } 30 "端口 $Port 释放"
}

$before = @(Get-ChildItem $appDir -Recurse -File | ForEach-Object FullName)
Start-Process $exe -ArgumentList '--no-browser'
Wait-Until { Test-Health } 180 '中台就绪'
if (Test-Path "$Root\data\config\model.local.yaml") {
    Wait-Until { (Get-Procs '*triage_worker.py*').Count -ge 1 } 60 'triage worker 拉起'
}
Get-Procs '*' | Select-Object ProcessId, ParentProcessId, Name | Format-Table | Out-String | Write-Host
$dirs = @('data\state', 'data\build', 'shell')   # 首启 prepare 重建索引;包内不带知识库,data\knowledge 由使用者放
foreach ($dir in $dirs) {
    if (-not (Test-Path (Join-Path $Root $dir))) { Show-Logs; throw "缺少 $dir(数据根没注入?)" }
}
$after = @(Get-ChildItem $appDir -Recurse -File | ForEach-Object FullName)
$added = @($after | Where-Object { $before -notcontains $_ -and $_ -notmatch '\\__pycache__\\' })
if ($added.Count) { throw "app 目录不应被写入: $($added -join ', ')" }

Start-Process $exe -ArgumentList '--no-browser'
Start-Sleep -Seconds 8
if ((Get-Procs '*web_server.py*').Count -ne 1) { Show-Logs; throw '二次启动拉起了第二个中台' }
# 按进程名数外壳:面板的 WebView2 进程命令行里也带 OpsAgent.exe 与安装路径
if (@(Get-Procs '*' | Where-Object Name -eq 'OpsAgent.exe').Count -ne 1) { Show-Logs; throw '二次启动留下了第二个外壳' }

Quit-Shell
Start-Process $exe -ArgumentList '--no-browser'
Wait-Until { Test-Health } 120 '再次启动后中台就绪'
Quit-Shell
Write-Host '冒烟通过'
