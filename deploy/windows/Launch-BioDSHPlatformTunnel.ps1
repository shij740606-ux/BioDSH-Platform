[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RunnerPath,

    [Parameter(Mandatory = $true)]
    [string]$ConfigPath
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$resolvedRunnerPath = (Resolve-Path -LiteralPath $RunnerPath).Path
$resolvedConfigPath = (Resolve-Path -LiteralPath $ConfigPath).Path
& $resolvedRunnerPath -ConfigPath $resolvedConfigPath -ValidateOnly

$arguments = @(
    '-NoProfile',
    '-NonInteractive',
    '-ExecutionPolicy', 'Bypass',
    '-File', ('"{0}"' -f $resolvedRunnerPath),
    '-ConfigPath', ('"{0}"' -f $resolvedConfigPath)
)
Start-Process -FilePath 'powershell.exe' -ArgumentList $arguments -WindowStyle Hidden
