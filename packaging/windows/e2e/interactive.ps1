param([Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][string]$Exe, [string]$Arguments = '',
      [string]$WorkingDirectory = (Split-Path $Exe), [int]$WaitSeconds = 0)
# 从 SSH 会话(session 0)把程序拉到当前用户的交互桌面:Interactive 计划任务,不需要密码;等待模式结束后删任务
$ProgressPreference = 'SilentlyContinue'
$ErrorActionPreference = 'Stop'
$action = New-ScheduledTaskAction -Execute $Exe -Argument $Arguments -WorkingDirectory $WorkingDirectory
$principal = New-ScheduledTaskPrincipal -UserId "$env:COMPUTERNAME\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $Name -Action $action -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $Name
if ($WaitSeconds -gt 0) {
    $deadline = (Get-Date).AddSeconds($WaitSeconds)
    do { Start-Sleep -Milliseconds 500; $task = Get-ScheduledTask -TaskName $Name } while ($task.State -eq 'Running' -and (Get-Date) -lt $deadline)
    "task=$Name state=$($task.State) result=$((Get-ScheduledTaskInfo -TaskName $Name).LastTaskResult)"
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false
} else {
    # 常驻程序(外壳)的任务留着,删任务可能连带结束它;测完由 remote.sh cleanup 统一删
    Start-Sleep -Seconds 2
    "task=$Name state=$((Get-ScheduledTask -TaskName $Name).State)"
}
