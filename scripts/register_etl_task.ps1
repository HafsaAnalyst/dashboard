# Registers a Windows Scheduled Task that runs the incremental ETL every 10 min.
# Run once, manually, as the current user. The task itself runs in the
# background under the same user with normal priority.
#
# To remove: Unregister-ScheduledTask -TaskName "TheMigration-ETL-10min" -Confirm:$false

$TaskName  = "TheMigration-ETL-10min"
$RepoRoot  = "C:\Users\DELL\Documents\Hafsa Saleh\IdeaProjects\Themigration\Project2"
$Python    = "$RepoRoot\.venv\Scripts\python.exe"
$EtlScript = "$RepoRoot\migration-dashboard\etl\run_etl.py"

if (-not (Test-Path $Python))    { throw "python.exe not found at $Python" }
if (-not (Test-Path $EtlScript)) { throw "ETL script not found at $EtlScript" }

# Remove any prior task with this name so the script is rerunnable
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed existing task: $TaskName"
}

$action = New-ScheduledTaskAction `
    -Execute  $Python `
    -Argument "`"$EtlScript`"" `
    -WorkingDirectory $RepoRoot

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 10) `
    -RepetitionDuration ([System.TimeSpan]::MaxValue)

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal `
    -UserId    $env:USERNAME `
    -LogonType S4U `
    -RunLevel  Limited

Register-ScheduledTask `
    -TaskName  $TaskName `
    -Action    $action `
    -Trigger   $trigger `
    -Settings  $settings `
    -Principal $principal `
    -Description "Runs The Migration's ETL pipeline every 10 minutes (incremental 2-day window)."

Write-Host ""
Write-Host "Registered scheduled task: $TaskName" -ForegroundColor Green
Write-Host "  First run: ~1 minute from now"
Write-Host "  Then every 10 minutes thereafter"
Write-Host ""
Write-Host "To watch logs:"
Write-Host "  Get-Content '$RepoRoot\migration-dashboard\etl.log' -Tail 40 -Wait"
