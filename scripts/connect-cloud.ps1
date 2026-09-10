param(
    [Parameter(Mandatory = $true)][string]$Server,
    [ValidateRange(1, 65535)][int]$LocalPort = 19876
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$identityPath = Join-Path $projectRoot '.opssentinel\cloud-ssh\id_ed25519'
$knownHostsPath = Join-Path $projectRoot '.opssentinel\cloud-ssh\known_hosts'
foreach ($requiredPath in @($identityPath, $knownHostsPath)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "Missing dedicated SSH file: $requiredPath"
    }
}
if (Get-NetTCPConnection -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue) {
    throw "Local port $LocalPort is already listening. The existing tunnel may still be running."
}
$sshArgs = @(
    '-N', '-o', 'KexAlgorithms=curve25519-sha256',
    '-o', 'StrictHostKeyChecking=yes', '-o', "UserKnownHostsFile=$knownHostsPath",
    '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
    '-o', 'ConnectTimeout=10', '-o', 'ExitOnForwardFailure=yes',
    '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3',
    '-i', $identityPath, '-L', "127.0.0.1:${LocalPort}:127.0.0.1:9876", $Server
)
Write-Host "Connecting $Server to local port $LocalPort. Keep this terminal open; Ctrl+C stops the tunnel."
& ssh @sshArgs
exit $LASTEXITCODE
