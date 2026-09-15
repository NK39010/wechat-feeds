# 注册 Windows 计划任务：每小时同步一次公众号文章并推送
# 用法（在本目录的 PowerShell 里）：  powershell -ExecutionPolicy Bypass -File .\register_task.ps1
# 取消：  Unregister-ScheduledTask -TaskName wechat-feeds-sync -Confirm:$false

$root = $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$log = Join-Path $root "sync.log"

# 通过 cmd 包一层，把输出追加到 sync.log，方便排查
$action = New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument "/c chcp 65001 >nul && `"$python`" sync.py >> `"$log`" 2>&1" `
    -WorkingDirectory $root

# 开机登录后 5 分钟开始，之后每小时一次
$trigger = New-ScheduledTaskTrigger -AtLogOn
$trigger.Delay = "PT5M"
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours 1)).Repetition

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "wechat-feeds-sync" -Action $action -Trigger $trigger -Settings $settings `
    -Description "We-MP-RSS -> reMarkable feeds (wechat-feeds/sync.py)" -Force | Out-Null

Write-Host "已注册计划任务 wechat-feeds-sync：登录后每小时运行一次，日志在 $log"
