[CmdletBinding()]
param(
    [ValidateRange(1, 65535)][int]$Port = 8765,
    [string]$DataDir = ''
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$VenvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$PythonArgs = @()
if (Test-Path -LiteralPath $VenvPython -PathType Leaf) {
    $PythonExe = $VenvPython
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $PythonExe = (Get-Command python).Source
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $PythonExe = (Get-Command py).Source
    $PythonArgs = @('-3')
} else {
    throw 'Python 3.11+ was not found. Install Python, create .venv, then install this project.'
}

Push-Location -LiteralPath $ProjectRoot
try {
    & $PythonExe @PythonArgs -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 or newer is required.' }
    & $PythonExe @PythonArgs -c 'import fastapi, uvicorn, httpx, pydantic, yaml, psutil' 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'Dependencies are missing. Run these commands from the project root:'
        if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
            $DisplayPython = $PythonExe.Replace("'", "''")
            $DisplayArgs = if ($PythonArgs.Count) { ' ' + ($PythonArgs -join ' ') } else { '' }
            Write-Host "& '$DisplayPython'$DisplayArgs -m venv .venv"
        }
        Write-Host "& '.\.venv\Scripts\python.exe' -m pip install -e '.[dev]'"
        exit 1
    }
    if ([string]::IsNullOrWhiteSpace($DataDir)) {
        $DataDir = Join-Path $ProjectRoot '.opssentinel'
    }
    Write-Host "OpsSentinel isolated demo: http://127.0.0.1:$Port (Ctrl+C to stop)"
    & $PythonExe @PythonArgs -m opssentinel --host 127.0.0.1 --port $Port --data-dir $DataDir --demo
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
