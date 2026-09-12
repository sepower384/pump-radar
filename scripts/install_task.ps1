# PUMP RADAR — 윈도우 작업 스케줄러 등록 (창 안 뜸)
#   .\install_task.ps1            # 로그인 시 자동시작 + 5분마다 살아있는지 확인
#   .\install_task.ps1 -Remove    # 등록 해제
param([switch]$Remove)

$ErrorActionPreference = 'Stop'
$TaskName = 'PumpRadar'
$Root = Split-Path -Parent $PSScriptRoot
$Vbs = Join-Path $Root 'scripts\run_hidden.vbs'

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "제거 완료: $TaskName"
    } else {
        Write-Host "등록된 작업이 없습니다."
    }
    return
}

if (-not (Test-Path $Vbs)) { throw "숨김 실행 파일이 없습니다: $Vbs" }

# 액션은 반드시 wscript.exe + vbs (cmd.exe /c 로 걸면 검은 창이 깜빡인다)
$Action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$Vbs`"" -WorkingDirectory $Root

$Triggers = @(
    (New-ScheduledTaskTrigger -AtLogOn),
    (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes 30))
)

$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)

$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Triggers `
    -Settings $Settings -Principal $Principal `
    -Description 'BTC 우상향 + 급등 코인/주식 슬랙 알림 (창 없이 백그라운드)' | Out-Null

Start-ScheduledTask -TaskName $TaskName
Write-Host "등록 + 시작 완료: $TaskName"
Write-Host "로그: $Root\data\watch.log"
Write-Host "중지: .\stop_watch.ps1  /  해제: .\install_task.ps1 -Remove"
