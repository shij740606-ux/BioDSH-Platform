[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,

    [string]$RunnerPath = (Join-Path $PSScriptRoot 'Start-BioDSHPlatformTunnel.ps1'),

    [string]$TaskName = 'BioDSH Platform Tunnel'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$resolvedRunnerPath = (Resolve-Path -LiteralPath $RunnerPath).Path
$resolvedConfigPath = (Resolve-Path -LiteralPath $ConfigPath).Path
& $resolvedRunnerPath -ConfigPath $resolvedConfigPath -ValidateOnly

$arguments = @(
    '-NoProfile',
    '-NonInteractive',
    '-WindowStyle', 'Hidden',
    '-ExecutionPolicy', 'Bypass',
    '-File', ('"{0}"' -f $resolvedRunnerPath),
    '-ConfigPath', ('"{0}"' -f $resolvedConfigPath)
) -join ' '

$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Days 3650) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description 'Maintain the configured BioDSH platform SSH tunnel and reconnect after interruption.' `
    -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName
