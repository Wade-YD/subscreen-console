# =========================================================
#  副屏触控开关 · 控制台 v1
#  原理: 禁用/启用 ARZOPA 便携屏的 USB 触摸数字化器 (HID 设备)
#  依赖: 无 (Windows 10 2004+ 自带 pnputil /disable-device)
#  权限: 管理员 (脚本启动时自动弹 UAC)
# =========================================================

param([switch]$JustParse)

$ErrorActionPreference = 'Stop'

# ===== 配置区 =====
# 副屏触摸芯片硬件 ID (VID_1A86 = 沁恒, ARZOPA 触摸通道)。以后换屏改这里即可。
$TouchHwIdPattern = 'VID_1A86&PID_E5E3'
# 兜底匹配: USB 总线上所有 HID 触摸屏设备 (用法页 0x000D = Digitizers)
$UsbTouchPattern  = '^HID\\VID_'
$DigitizerHwPattern = 'UP:000D_U:000[14]'
# ==================

# ---- 自提权: 非管理员则弹 UAC 重启自己 (控制台隐藏) ----
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($id)
if (-not $JustParse -and -not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    try {
        Start-Process -FilePath 'powershell.exe' -Verb RunAs -WindowStyle Hidden `
            -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File',('"{0}"' -f $PSCommandPath))
    } catch {
        Add-Type -AssemblyName PresentationFramework
        [System.Windows.MessageBox]::Show('未获得管理员权限，面板未启动。','副屏触控开关') | Out-Null
    }
    exit
}

# ---- 单实例互斥: 已有面板在跑则退出 ----
$script:mutex = New-Object System.Threading.Mutex($false, 'TouchTogglePanel')
$script:mutexOwned = $false
try { $script:mutexOwned = $script:mutex.WaitOne(0) } catch { $script:mutexOwned = $false }
if (-not $script:mutexOwned) { exit }

Add-Type -AssemblyName PresentationFramework

# ---- 设备探测 ----
function Get-TouchDevices {
    $all = @(Get-PnpDevice -Class HIDClass -ErrorAction SilentlyContinue |
        Where-Object { $_.Status -ne 'Unknown' -and $_.InstanceId -match $UsbTouchPattern } |
        Where-Object {
            ($_.FriendlyName -match '触摸屏|touch screen') -or
            (($_.HardwareID -join ' ') -match $DigitizerHwPattern)
        })
    # 优先取配置区指定的副屏触摸芯片
    $hit = @($all | Where-Object { ($_.InstanceId -match $TouchHwIdPattern) -or (($_.HardwareID -join ' ') -match $TouchHwIdPattern) })
    if ($hit.Count -gt 0) { return ,$hit }
    return ,$all
}

# 设备状态三态: 0=正常 21=重启过渡(视为开启) 22=已禁用 其他=设备自身异常(开关管不了)
function Get-TouchState([object]$dev) {
    $pc = (Get-PnpDeviceProperty -InstanceId $dev.InstanceId -KeyName 'DEVPKEY_Device_ProblemCode' -ErrorAction SilentlyContinue).Data
    if ($null -eq $pc) { $pc = if ($dev.Status -eq 'OK') { 0 } else { -1 } }
    return [pscustomobject]@{ Enabled = ($pc -eq 0 -or $pc -eq 21); ProblemCode = $pc }
}

function Set-TouchState([string]$InstanceId, [bool]$Enable) {
    $op = if ($Enable) { '/enable-device' } else { '/disable-device' }
    $out = & pnputil.exe $op $InstanceId 2>&1
    if ($LASTEXITCODE -ne 0) {
        # 回退方案: CIM 方式禁用/启用
        $dev = Get-PnpDevice -InstanceId $InstanceId -ErrorAction Stop
        if ($Enable) { $dev | Enable-PnpDevice -Confirm:$false -ErrorAction Stop }
        else         { $dev | Disable-PnpDevice -Confirm:$false -ErrorAction Stop }
        $out = @($out) + ('[回退] CIM 方式' + $(if ($Enable) { '启用' } else { '禁用' }) + '成功')
    }
    Start-Sleep -Milliseconds 400
    return (($out | Out-String).Trim())
}

function Hex([string]$c) {
    [System.Windows.Media.SolidColorBrush][System.Windows.Media.ColorConverter]::ConvertFromString($c)
}

# ---- 界面 ----
$xaml = @'
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="副屏触控开关" Height="310" Width="430"
        WindowStartupLocation="CenterScreen" ResizeMode="NoResize"
        Background="#1E1E2E" FontFamily="Microsoft YaHei UI">
  <StackPanel Margin="24,20">
    <TextBlock Text="ARZOPA 副屏 · 触摸开关" Foreground="#CDD6F4" FontSize="15" FontWeight="Bold" HorizontalAlignment="Center"/>
    <TextBlock x:Name="TxtStatus" Text="检测中…" Foreground="#CDD6F4" FontSize="26" FontWeight="Bold" HorizontalAlignment="Center" Margin="0,20,0,0"/>
    <TextBlock x:Name="TxtDevice" Text="" Foreground="#6C7086" FontSize="11" TextWrapping="Wrap" HorizontalAlignment="Center" Margin="0,6,0,0"/>
    <Button x:Name="BtnToggle" Content="触摸开关" FontSize="17" FontWeight="Bold" Padding="0,12" Margin="0,20,0,0"
            Background="#F38BA8" Foreground="#FFFFFF" BorderThickness="0" Cursor="Hand"/>
    <Grid Margin="0,14,0,0">
      <Grid.ColumnDefinitions>
        <ColumnDefinition Width="*"/>
        <ColumnDefinition Width="*"/>
      </Grid.ColumnDefinitions>
      <Button x:Name="BtnRefresh" Content="刷新状态" Grid.Column="0" Margin="0,0,5,0" Padding="0,8"
              Background="#313244" Foreground="#CDD6F4" BorderThickness="0" Cursor="Hand"/>
      <Button x:Name="BtnExit" Content="退出" Grid.Column="1" Margin="5,0,0,0" Padding="0,8"
              Background="#313244" Foreground="#CDD6F4" BorderThickness="0" Cursor="Hand"/>
    </Grid>
    <TextBlock x:Name="TxtLog" Text="" Foreground="#94A3B8" FontSize="11" TextWrapping="Wrap" MaxHeight="42" TextTrimming="CharacterEllipsis" Margin="0,12,0,0"/>
  </StackPanel>
</Window>
'@

try {
$window = [System.Windows.Markup.XamlReader]::Parse($xaml)
$TxtStatus  = $window.FindName('TxtStatus')
$TxtDevice  = $window.FindName('TxtDevice')
$TxtLog     = $window.FindName('TxtLog')
$BtnToggle  = $window.FindName('BtnToggle')
$BtnRefresh = $window.FindName('BtnRefresh')
$BtnExit    = $window.FindName('BtnExit')
$script:dev = $null

function Update-Panel {
    $devs = Get-TouchDevices
    if ($devs.Count -eq 0) {
        $script:dev = $null
        $TxtStatus.Text = '未找到触摸设备'
        $TxtStatus.Foreground = Hex '#FAB387'
        $TxtDevice.Text = '请检查副屏 USB 线后点 [刷新状态]'
        $BtnToggle.IsEnabled = $false
        $BtnToggle.Content = '触摸开关'
        $BtnToggle.Background = Hex '#313244'
        return
    }
    $script:dev = $devs[0]
    $extra = if ($devs.Count -gt 1) { "（共 $($devs.Count) 个触摸设备，正在控制第 1 个）" } else { '' }
    $st = Get-TouchState $script:dev
    $TxtDevice.Text = $script:dev.InstanceId + $extra
    if ($st.Enabled) {
        $TxtStatus.Text = '副屏触摸：已开启'
        $TxtStatus.Foreground = Hex '#A6E3A1'
        $BtnToggle.Content = '关闭副屏触摸'
        $BtnToggle.Background = Hex '#F38BA8'
        $BtnToggle.IsEnabled = $true
    } elseif ($st.ProblemCode -eq 22) {
        $TxtStatus.Text = '副屏触摸：已关闭'
        $TxtStatus.Foreground = Hex '#F38BA8'
        $BtnToggle.Content = '开启副屏触摸'
        $BtnToggle.Background = Hex '#A6E3A1'
        $BtnToggle.IsEnabled = $true
    } else {
        $TxtStatus.Text = '触摸设备异常'
        $TxtStatus.Foreground = Hex '#FAB387'
        $TxtDevice.Text = "ProblemCode=$($st.ProblemCode)：设备自身异常（多为驱动问题），触摸开关管不了，请到设备管理器检查该设备"
        $BtnToggle.Content = '触摸开关不可用'
        $BtnToggle.Background = Hex '#313244'
        $BtnToggle.IsEnabled = $false
    }
}

$BtnToggle.Add_Click({
    if (-not $script:dev) { return }
    $fresh = Get-PnpDevice -InstanceId $script:dev.InstanceId -ErrorAction SilentlyContinue
    if (-not $fresh -or $fresh.Status -eq 'Unknown') { Update-Panel; return }
    $BtnToggle.IsEnabled = $false
    $enable = -not (Get-TouchState $script:dev).Enabled
    $TxtStatus.Text = '切换中…'
    $TxtStatus.UpdateLayout()
    try { $window.Dispatcher.Invoke([action]{}, 'Background') | Out-Null } catch {}
    try {
        $log = Set-TouchState $script:dev.InstanceId $enable
        Start-Sleep -Milliseconds 300
        Update-Panel
        $TxtLog.Text = ($log -replace "\r?\n", '  |  ')
    } catch {
        $TxtLog.Text = '失败：' + $_.Exception.Message
        Update-Panel
    }
})

$BtnRefresh.Add_Click({
    Update-Panel
    $TxtLog.Text = '状态已刷新 ' + (Get-Date -Format 'HH:mm:ss')
})

$BtnExit.Add_Click({ $window.Close() })

Update-Panel
if ($JustParse) {
    $en = if ($script:dev) { (Get-TouchState $script:dev).Enabled } else { 'no-device' }
    "JUSTPARSE OK | status=[$($TxtStatus.Text)] | dev=[$($script:dev.InstanceId)] | enabled=$en"
    $window.Close()
    exit 0
}
[void]$window.ShowDialog()
} catch {
    if ($JustParse) { Write-Output ('JUSTPARSE FATAL: ' + $_.ToString()); exit 1 }
    [System.Windows.MessageBox]::Show(('启动失败：' + [Environment]::NewLine + $_.Exception.Message), '副屏触控开关') | Out-Null
    exit 1
}
