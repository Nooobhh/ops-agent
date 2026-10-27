param([Parameter(Mandatory)][string]$Root, [Parameter(Mandatory)][string]$Action, [string]$Arg = '', [int]$Wait = 2, [int]$Port = 8420)
# 在用户交互桌面里执行(经 interactive.ps1 + hidden.vbs 拉起):真实鼠标点托盘、UIA 找面板按钮、截屏。
# 结果写 <Root>\e2e-out\out-<Action>.txt 与 shot-*.png,由 remote.sh 取回。
$ProgressPreference = 'SilentlyContinue'
$ErrorActionPreference = 'Stop'
$outDir = Join-Path $Root 'e2e-out'
New-Item -ItemType Directory -Force $outDir | Out-Null
$out = Join-Path $outDir "out-$Action.txt"
Add-Type -AssemblyName System.Windows.Forms, System.Drawing, UIAutomationClient, UIAutomationTypes
Add-Type @'
using System; using System.Runtime.InteropServices;
public static class U {
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr FindWindow(string cls, string title);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr FindWindowEx(IntPtr parent, IntPtr after, string cls, string title);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint dx, uint dy, uint d, UIntPtr e);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("shell32.dll")] public static extern int Shell_NotifyIconGetRect(ref NII id, out RECT r);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
  [StructLayout(LayoutKind.Sequential)] public struct NII { public uint cbSize; public IntPtr hWnd; public uint uID; public Guid guid; }
  public static void Click(int x, int y, bool right) {
    SetCursorPos(x, y); System.Threading.Thread.Sleep(120);
    uint down = right ? 0x0008u : 0x0002u, up = right ? 0x0010u : 0x0004u;
    mouse_event(down, 0, 0, 0, UIntPtr.Zero); System.Threading.Thread.Sleep(80);
    mouse_event(up, 0, 0, 0, UIntPtr.Zero);
  }
}
'@
[U]::SetProcessDPIAware() | Out-Null
$log = New-Object System.Collections.Generic.List[string]
function Note([string]$s) { $log.Add("$(Get-Date -Format HH:mm:ss.fff) $s") }

function Panel { [U]::FindWindow('OpsAgentPanel', [NullString]::Value) }
function MsgWindow { [U]::FindWindow('OpsAgentShellMsg', [NullString]::Value) }  # 托盘图标挂在消息窗口上;PowerShell 传 $null 给 string 会变成空串,须用 NullString
function RectOf([IntPtr]$h) { $r = New-Object U+RECT; [U]::GetWindowRect($h, [ref]$r) | Out-Null; $r }
function IconRect {
  $id = New-Object U+NII
  $id.cbSize = [Runtime.InteropServices.Marshal]::SizeOf([type][U+NII]); $id.hWnd = MsgWindow; $id.uID = 1
  $r = New-Object U+RECT
  $hr = [U]::Shell_NotifyIconGetRect([ref]$id, [ref]$r)
  @{ hr = $hr; rect = $r }
}
function Center($r) { @([int](($r.L + $r.R) / 2), [int](($r.T + $r.B) / 2)) }
function Shot([string]$name) {
  $b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
  $bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size)
  $bmp.Save((Join-Path $outDir "shot-$name.png"), [System.Drawing.Imaging.ImageFormat]::Png)
  $g.Dispose(); $bmp.Dispose()
}
function Health {
  try { (Invoke-RestMethod "http://127.0.0.1:$Port/api/health" -TimeoutSec 2).status } catch { 'down' }
}
function Procs { @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*$Root*" }) }
function Info([string]$tag) {
  $p = Panel
  $procs = Procs
  $ir = IconRect
  $vis = if ($p -ne [IntPtr]::Zero) { [U]::IsWindowVisible($p) } else { $false }
  Note ("[{0}] panel_hwnd={1} visible={2} foreground={3} panel_rect={4} icon_hr={5} icon_rect={6} health={7}" -f `
    $tag, $p, $vis, ($p -ne [IntPtr]::Zero -and [U]::GetForegroundWindow() -eq $p), ((RectOf $p) | ConvertTo-Json -Compress),
    $ir.hr, ($ir.rect | ConvertTo-Json -Compress), (Health))
  $webview = @(Get-CimInstance Win32_Process -Filter "Name='msedgewebview2.exe'" | Where-Object { $_.CommandLine -like "*$Root*" })
  Note ("[{0}] shell={1} web_server={2} triage_worker={3} webview2={4}" -f $tag,
    @($procs | Where-Object Name -eq 'OpsAgent.exe').Count,
    @($procs | Where-Object { $_.CommandLine -like '*web_server.py*' }).Count,
    @($procs | Where-Object { $_.CommandLine -like '*triage_worker.py*' }).Count, $webview.Count)
}
# 图标在溢出区(^)时先展开溢出区再取图标位置;上次残留展开的溢出区先收起,免得点到过期坐标
function Chevron {
  $tray = [U]::FindWindow('Shell_TrayWnd', [NullString]::Value)
  $notify = [U]::FindWindowEx($tray, [IntPtr]::Zero, 'TrayNotifyWnd', [NullString]::Value)
  Center (RectOf ([U]::FindWindowEx($notify, [IntPtr]::Zero, 'Button', [NullString]::Value)))
}
function ClickTray([bool]$right) {
  $overflow = [U]::FindWindow('NotifyIconOverflowWindow', [NullString]::Value)
  if ([U]::IsWindowVisible($overflow)) {
    $c = Chevron; Note "overflow left open; close via chevron at $c"
    [U]::Click($c[0], $c[1], $false); Start-Sleep -Milliseconds 600
  }
  $ir = IconRect
  # Shell_NotifyIconGetRect 对溢出区里(未展开)的图标返回 S_FALSE,矩形是 ^ 按钮
  # 溢出层偶尔点一次没展开(或刚被别的窗口抢走焦点收起),最多试三次
  for ($try = 1; $try -le 3 -and $ir.hr -ne 0; $try++) {
    $c = Chevron; Note "icon in overflow (hr=$($ir.hr)); open via chevron at $c (try $try)"
    [U]::Click($c[0], $c[1], $false); Start-Sleep -Milliseconds (800 * $try)
    $ir = IconRect
  }
  if ($ir.hr -ne 0) { throw "取不到托盘图标位置(hr=$($ir.hr))" }
  $c = Center $ir.rect
  Note "click tray icon right=$right at $c"
  [U]::Click($c[0], $c[1], $right)
}
function FindByName([string]$name, [string]$controlType, [IntPtr]$scope) {
  $rootEl = if ($scope -eq [IntPtr]::Zero) { [System.Windows.Automation.AutomationElement]::RootElement } else { [System.Windows.Automation.AutomationElement]::FromHandle($scope) }
  $cond = New-Object System.Windows.Automation.AndCondition(
    (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty, $name)),
    (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::$controlType)))
  $el = $null
  for ($i = 0; $i -lt 20 -and -not $el; $i++) { $el = $rootEl.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond); if (-not $el) { Start-Sleep -Milliseconds 250 } }
  if (-not $el) { throw "UIA 未找到 $controlType '$name'" }
  $el
}
function ClickByName([string]$name, [string]$controlType = 'Button', [IntPtr]$scope = [IntPtr]::Zero) {
  $el = FindByName $name $controlType $scope
  $b = $el.Current.BoundingRectangle
  $x = [int]($b.X + $b.Width / 2); $y = [int]($b.Y + $b.Height / 2)
  Note "click $controlType '$name' enabled=$($el.Current.IsEnabled) at ($x,$y)"
  [U]::Click($x, $y, $false)
}
function TypeInto([string]$label, [string]$text) {
  $el = FindByName $label 'Edit' (Panel)
  $b = $el.Current.BoundingRectangle
  [U]::Click([int]($b.X + $b.Width / 2), [int]($b.Y + $b.Height / 2), $false)
  [System.Windows.Forms.SendKeys]::SendWait('^a{DEL}')
  [System.Windows.Forms.SendKeys]::SendWait(($text -replace '([+^%~(){}\[\]])', '{$1}'))
  Note "type into '$label' ($($text.Length) chars)"
}
function EnsurePanel { $p = Panel; if ($p -eq [IntPtr]::Zero -or -not [U]::IsWindowVisible($p)) { ClickTray $false; Start-Sleep -Seconds 2 } }

try {
  switch -Regex ($Action) {
    '^info$'   { Info 'now'; Shot 'info' }
    '^tray$'   { Info 'before'; $t0 = Get-Date; ClickTray $false
                 for ($i = 0; $i -lt 50 -and -not ((Panel) -ne [IntPtr]::Zero -and [U]::IsWindowVisible((Panel))); $i++) { Start-Sleep -Milliseconds 100 }
                 Note ("panel visible after {0} ms" -f [int]((Get-Date) - $t0).TotalMilliseconds)
                 Start-Sleep -Seconds $Wait; Info 'after'; Shot 'tray' }
    '^toggle$' {
      # 同一脚本内连续操作,避免拉起测试脚本本身抢焦点干扰判断
      Info 'start'
      ClickTray $false; Start-Sleep -Seconds 2; Info 'tray-1(应显示)'; Shot 'toggle-1'
      # 点任务栏中间的空白处让面板失焦;不点桌面窗口区域,免得误点到用户开着的程序
      $b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
      [U]::Click([int]($b.Width / 2), $b.Height - 15, $false); Start-Sleep -Seconds 1; Info 'away(应隐藏)'
      ClickTray $false; Start-Sleep -Seconds 1; Info 'tray-2(应显示)'; Shot 'toggle-2'
      [System.Windows.Forms.SendKeys]::SendWait('{ESC}'); Start-Sleep -Seconds 1; Info 'esc(应隐藏)'
    }
    '^button$' { EnsurePanel; Info 'before'; ClickByName $Arg 'Button' (Panel); Start-Sleep -Seconds $Wait; Info 'after'; Shot "button" }
    '^check$'  { EnsurePanel; ClickByName $Arg 'CheckBox' (Panel); Start-Sleep -Seconds $Wait; Info 'after'; Shot 'check' }
    '^config$' {
      # Arg = repo|token:更新设置里填仓库与 token(token 不进日志)
      EnsurePanel
      $repo, $token = $Arg -split '\|', 2
      ClickByName '更新设置' 'Button' (Panel); Start-Sleep -Milliseconds 600
      TypeInto '发布仓(owner/name)' $repo
      if ($token) { TypeInto 'token(只读该仓)' $token }
      ClickByName '保存' 'Button' (Panel); Start-Sleep -Seconds $Wait; Info 'after'; Shot 'config'
    }
    '^menu$'   {
      Info 'before'; ClickTray $true; Start-Sleep -Seconds 1; Shot 'menu-open'
      if ($Arg) { ClickByName $Arg 'MenuItem'; Start-Sleep -Seconds $Wait }
      Info 'after'; Shot 'menu'
    }
    '^shot$'   { Shot $Arg }
    '^chevron$' { $c = Chevron; [U]::Click($c[0], $c[1], $false); Start-Sleep -Milliseconds 700; Note ("overflow visible=" + [U]::IsWindowVisible([U]::FindWindow('NotifyIconOverflowWindow', [NullString]::Value)) + " fg=" + [U]::GetForegroundWindow()); Shot 'chevron' }
  }
} catch {
  Note "ERROR: $($_.Exception.Message)"
  try { Shot 'error' } catch {}
}
[IO.File]::WriteAllLines($out, $log, (New-Object System.Text.UTF8Encoding($false)))
