# Register a Windows scheduled task: sync WeChat articles and push, every hour.
# Usage (PowerShell in this folder):  powershell -ExecutionPolicy Bypass -File .\register_task.ps1
# Remove:  Unregister-ScheduledTask -TaskName wechat-feeds-sync -Confirm:$false

$root   = $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$log    = Join-Path $root 'sync.log'

# cmd wrapper appends output to sync.log for troubleshooting
$cmdArgs = '/c chcp 65001 >nul && set "PYTHONIOENCODING=utf-8" && "' + $python + '" sync.py >> "' + $log + '" 2>&1'
$action  = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument $cmdArgs -WorkingDirectory $root

# first run in 2 minutes, then every hour; missed runs (PC asleep/off) start as soon as possible
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Hours 1)

$settingsArgs = @{
    StartWhenAvailable         = $true
    AllowStartIfOnBatteries    = $true
    DontStopIfGoingOnBatteries = $true
    ExecutionTimeLimit         = (New-TimeSpan -Minutes 30)
    MultipleInstances          = 'IgnoreNew'
}
$settings = New-ScheduledTaskSettingsSet @settingsArgs

Register-ScheduledTask -TaskName 'wechat-feeds-sync' -Action $action -Trigger $trigger -Settings $settings -Description 'We-MP-RSS -> reMarkable feeds (wechat-feeds/sync.py)' -Force | Out-Null
Write-Host "Registered scheduled task 'wechat-feeds-sync' (hourly). Log: $log"