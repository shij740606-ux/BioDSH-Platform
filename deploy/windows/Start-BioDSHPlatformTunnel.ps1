[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,

    [switch]$ValidateOnly
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$resolvedConfigPath = (Resolve-Path -LiteralPath $ConfigPath).Path
$config = Get-Content -LiteralPath $resolvedConfigPath -Raw | ConvertFrom-Json

$requiredProperties = @(
    'sshHost',
    'sshPort',
    'sshUser',
    'localAddress',
    'localPort',
    'remoteAddress',
    'remotePort',
    'knownHostsPath',
    'retrySeconds'
)
foreach ($property in $requiredProperties) {
    if ($null -eq $config.$property -or [string]::IsNullOrWhiteSpace([string]$config.$property)) {
        throw "Missing tunnel configuration property: $property"
    }
}

$sshExecutable = if ($config.sshExecutable) {
    [Environment]::ExpandEnvironmentVariables([string]$config.sshExecutable)
} else {
    Join-Path $env:SystemRoot 'System32\OpenSSH\ssh.exe'
}
$knownHostsPath = [Environment]::ExpandEnvironmentVariables([string]$config.knownHostsPath)

foreach ($portProperty in @('sshPort', 'localPort', 'remotePort')) {
    $port = [int]$config.$portProperty
    if ($port -lt 1 -or $port -gt 65535) {
        throw "Invalid port in tunnel configuration: $portProperty"
    }
}
if ([int]$config.retrySeconds -lt 1) {
    throw 'retrySeconds must be greater than zero'
}

$forward = '{0}:{1}:{2}:{3}' -f $config.localAddress, $config.localPort, $config.remoteAddress, $config.remotePort
$destination = '{0}@{1}' -f $config.sshUser, $config.sshHost
$arguments = @(
    '-N',
    '-T',
    '-L', $forward,
    '-p', [string]$config.sshPort,
    '-o', 'ExitOnForwardFailure=yes',
    '-o', 'ServerAliveInterval=30',
    '-o', 'ServerAliveCountMax=3',
    '-o', 'StrictHostKeyChecking=yes',
    '-o', "UserKnownHostsFile=$knownHostsPath",
    $destination
)

if ($ValidateOnly) {
    return
}

$mutexKeyBytes = [Text.Encoding]::UTF8.GetBytes($resolvedConfigPath.ToLowerInvariant())
$sha256 = [Security.Cryptography.SHA256]::Create()
try {
    $mutexKey = ([BitConverter]::ToString($sha256.ComputeHash($mutexKeyBytes))).Replace('-', '')
} finally {
    $sha256.Dispose()
}
$mutex = [Threading.Mutex]::new($false, "Local\BioDSHPlatformTunnel-$mutexKey")
if (-not $mutex.WaitOne(0)) {
    $mutex.Dispose()
    return
}

try {
    while ($true) {
        & $sshExecutable @arguments
        Start-Sleep -Seconds ([int]$config.retrySeconds)
    }
} finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
